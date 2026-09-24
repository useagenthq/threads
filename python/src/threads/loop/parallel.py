"""Parallel tool calls (spec/schema/README.md): the loop's step over pending calls. The next group
of concurrent reads starts together and is recorded in call order; any other call runs alone.

Python authorizes lazily, so a group's candidates are authorized first, in call order: its
`before_tool`/`permission_decision` events come before its first result."""

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from typing import Final

from threads.log import CallId, ToolSpec
from threads.loop import calls, effects, tool_gates
from threads.loop.groups import Candidate, Decision, groups, joins
from threads.loop.history import CallState, call_state, open_cancel
from threads.loop.runtime import Failed, Halt, Runtime, fence
from threads.loop.tools import Dispatched, Invocation
from threads.tools.specs import FRAMEWORK

WINDOW: Final = 8
"""At most this many calls of a group are started and not yet recorded."""


async def run_pending(rt: Runtime) -> Halt | None:
    """Runs the next group of pending calls, or the first pending call alone."""
    group = await _next_group(rt)
    if not isinstance(group, tuple):
        return group
    if len(group) > 1:
        return await run_group(rt, group)
    call_id = rt.fold.pending[0]
    halt = await calls.run_call(rt, call_id)
    return halt or await tool_gates.after_tool(rt, call_id)


async def _next_group(rt: Runtime) -> tuple[CallId, ...] | Halt:
    """Authorizes each call that could join, in call order, up to the first that isn't allowed;
    then plans the group from the first pending call."""
    candidates: list[Candidate] = []
    for call_id in rt.fold.pending:
        state = call_state(rt.events, call_id)
        spec = rt.fold.call_specs[state.call.data.call_id]
        found = _candidate(rt, state, spec)
        # Authorized only once it could join: other calls keep their lazy authorization.
        if found.decision == "none" and joins(replace(found, decision="allow")):
            halt = await calls.authorize(rt, state, spec)
            if halt is not None:
                return halt
            found = _candidate(rt, call_state(rt.events, call_id), spec)
        candidates.append(found)
        if not joins(found):
            break
    first = groups(candidates)[0] if candidates else ()
    return tuple(rt.fold.pending[i] for i in first)


def _candidate(rt: Runtime, state: CallState, spec: ToolSpec) -> Candidate:
    return Candidate(
        spec.name in rt.concurrent,
        spec.effect_class,
        spec.name in FRAMEWORK,
        spec.ends_turn is True,
        _decision(state),
    )


def _decision(state: CallState) -> Decision:
    """The recorded decision: an ask stays an ask after its approval, so it always runs alone."""
    if state.decision == "allow":
        return "allow"
    if state.decision == "ask":
        return "ask"
    if state.decision == "deny":
        return "deny"
    return "none"


async def run_group(rt: Runtime, group: Sequence[CallId]) -> Halt | None:
    """Starts the group's reads within the window and records each result, then after_tool, in
    call order. On a stale lease nothing more is recorded and the started bodies are cancelled
    and awaited, so none outlives the run."""
    invocations = [_invocation(rt, call_id) for call_id in group]
    started: list[asyncio.Task[Dispatched]] = []
    try:
        for i, inv in enumerate(invocations):
            stale = await _start(rt, invocations, started, i + WINDOW)
            if stale is not None:
                return stale
            if i >= len(started):
                return None  # a cancel: the queued calls never start
            halt = await calls.record_read(rt, inv, await started[i])
            halt = halt or await tool_gates.after_tool(rt, inv.call_id)
            if halt is not None:
                return halt
        return None
    finally:
        for task in started:
            task.cancel()
        await asyncio.gather(*started, return_exceptions=True)


def _invocation(rt: Runtime, call_id: CallId) -> Invocation:
    state = call_state(rt.events, call_id)
    return effects.invocation(state, rt.fold.call_specs[state.call.data.call_id])


async def _start(
    rt: Runtime,
    invocations: Sequence[Invocation],
    started: list[asyncio.Task[Dispatched]],
    limit: int,
) -> Failed | None:
    """Starts queued calls up to `limit`, unless a cancel is open. Each body starts eagerly right
    after its own fence, with no await between them."""
    loop = asyncio.get_running_loop()
    while len(started) < min(limit, len(invocations)):
        if open_cancel(rt.events) is not None:
            return None
        stale = await fence(rt)
        if stale is not None:
            return stale
        body = rt.tools.dispatch(invocations[len(started)])
        started.append(asyncio.eager_task_factory(loop, body))
    return None
