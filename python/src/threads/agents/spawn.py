"""spawn_agent: a subagent runs in a child thread with its own log.

`agent_spawned` is durable before the child's `thread_started`, so cancellation and recovery can
always find the child. A foreground child's one `agent_finished` feeds the call's result; a
background child's call gets a `deferred` placeholder at once and its result later as
`tool_result_late`. A crash mid-child resumes the same child thread by the id `agent_spawned`
recorded: it is never started twice.
"""

import asyncio
from typing import Final

from pydantic import JsonValue

from threads._generated.tools_v1 import SpawnAgentInput
from threads.agents import stops
from threads.agents.bindings import permissions
from threads.agents.children import finished, shown
from threads.agents.definition import Definition
from threads.agents.launch import Launch, Team
from threads.agents.results import Failed, Parked, RunResult
from threads.agents.scope import Scope
from threads.hooks.runner import STOP, SWITCH, decision_draft
from threads.log import (
    AgentFinishedData,
    AgentFinishedEvent,
    AgentSpawnedEvent,
    CallId,
    HookDecisionEvent,
    ThreadId,
)
from threads.loop.budget import inherited
from threads.loop.drafts import draft
from threads.loop.gates import MAX_STOP_CONTINUES, said, verdict
from threads.loop.history import CallState, open_cancel
from threads.loop.results import As, result_draft, text_ref
from threads.loop.runtime import Failed as HaltFailed
from threads.loop.runtime import Halt, Runtime, lost
from threads.result import Err
from threads.store.lines import uuid7

type Tasks = dict[ThreadId, asyncio.Task[None]]
type Ended = tuple[dict[str, JsonValue], str]
"""A child's agent_finished data and the spawn call's result text."""

_DEFERRED: Final = "started in background"


async def spawn[D](scope: Scope[D], rt: Runtime, state: CallState, tasks: Tasks) -> Halt | None:
    call_id = state.call.data.call_id
    args = SpawnAgentInput.model_validate(dict(state.call.data.input))
    spawned = _spawned(rt, call_id)
    if spawned is None:
        child = _child(scope, args.agent)
        why = _refusal(scope, child, args) or _running(rt, args.agent)
        if why is not None or child is None:
            return await _close(rt, call_id, why or f"unknown agent {args.agent}")
        halt = await _start(scope, rt, state, child, args)
        spawned = _spawned(rt, call_id)
        if halt is not None or spawned is None:
            return halt
    child = _child(scope, spawned.data.agent_name)
    if child is None:
        return await _close(rt, call_id, f"agent {spawned.data.agent_name} is not configured")
    if spawned.data.mode == "background":
        start_background(scope, rt, spawned, tasks)
        return None
    return await _foreground(rt, spawned, await _outcome(scope, rt, spawned, child, args.prompt))


async def _foreground(
    rt: Runtime, spawned: AgentSpawnedEvent, ended: Ended | Parked | HaltFailed
) -> Halt | None:
    """A foreground child's end: its agent_finished and the call's result, or the park."""
    if isinstance(ended, HaltFailed):
        return ended
    if isinstance(ended, Parked):
        return await stops.park(rt, spawned, ended)
    data, text = ended
    status = As("executed", data["status"] != "completed")
    result = await result_draft(rt, spawned.data.call_id, text, status)
    done = await rt.append(draft("agent_finished", data), result)
    return lost(done.error) if isinstance(done, Err) else None


def _spawned(rt: Runtime, call_id: str) -> AgentSpawnedEvent | None:
    return next(
        (e for e in rt.events if isinstance(e, AgentSpawnedEvent) and e.data.call_id == call_id),
        None,
    )


def _child[D](scope: Scope[D], name: str) -> Definition[None] | None:
    return next((d for d in scope.definition.subagents if d.name == name), None)


def _refusal[D](
    scope: Scope[D], child: Definition[None] | None, args: SpawnAgentInput
) -> str | None:
    """Pre-effect: an unlisted agent, or an isolation this child can't have."""
    if child is None:
        return f"unknown agent {args.agent}: not one of this agent's subagents"
    wanted = _isolation(child, args)
    if wanted not in ("none", "shared_sandbox"):
        return f"isolation {wanted} is not supported yet"
    if (wanted == "shared_sandbox") != (child.sandbox is not None):
        return f"isolation {wanted} doesn't fit agent {child.name}"
    if wanted == "shared_sandbox" and scope.shared is None:
        return "shared_sandbox needs the parent's sandbox"
    return None


def _running(rt: Runtime, name: str) -> str | None:
    """A member is its agent name: never two unfinished instances of one name (spec, Team tools)."""
    ended = {e.data.child_thread_id for e in rt.events if isinstance(e, AgentFinishedEvent)}
    live = any(
        isinstance(e, AgentSpawnedEvent)
        and e.data.agent_name == name
        and e.data.child_thread_id not in ended
        for e in rt.events
    )
    return f"member_active: {name} is still running" if live else None


def _isolation(child: Definition[None], args: SpawnAgentInput) -> str:
    if isinstance(args.isolation, str):
        return args.isolation
    return "none" if child.sandbox is None else "shared_sandbox"


async def _close(rt: Runtime, call_id: CallId, why: str) -> Halt | None:
    done = await rt.append(await result_draft(rt, call_id, why, As("not_executed", True)))
    return lost(done.error) if isinstance(done, Err) else None


async def _start[D](
    scope: Scope[D], rt: Runtime, state: CallState, child: Definition[None], args: SpawnAgentInput
) -> Halt | None:
    """subagent_start gates the spawn; its decision and agent_spawned are one batch."""
    call_id = state.call.data.call_id
    ids = {"call_id": call_id}
    ran = await rt.hooks.run("subagent_start", SWITCH, state.call.data)
    drafts = [
        decision_draft("subagent_start", r, verdict(r), said(r, "reason"), **ids) for r in ran
    ]
    if any(verdict(r) != "allow" for r in ran):
        why = "denied by subagent_start"
        drafts.append(await result_draft(rt, call_id, why, As("denied", True, "host")))
        done = await rt.append(*drafts)
        return lost(done.error) if isinstance(done, Err) else None
    background = args.background is True
    data: dict[str, JsonValue] = {
        "call_id": call_id,
        "child_thread_id": uuid7(rt.clock()),
        "agent_name": child.name,
        "mode": "background" if background else "foreground",
        "isolation": _isolation(child, args),
    }
    if child.budget is not None:
        data["budget"] = child.budget.model_dump(mode="json")
    drafts.append(draft("agent_spawned", data))
    if background:
        drafts.append(await result_draft(rt, call_id, _DEFERRED, As("deferred")))
    done = await rt.append(*drafts)
    return lost(done.error) if isinstance(done, Err) else None


def start_background[D](
    scope: Scope[D], rt: Runtime, spawned: AgentSpawnedEvent, tasks: Tasks
) -> None:
    """Runs the child beside the parent; its result lands as tool_result_late. Also restarts a
    background child a crash interrupted: the same child thread, never a second one."""
    child = _child(scope, spawned.data.agent_name)
    if spawned.data.child_thread_id in tasks or child is None:
        return
    prompt = SpawnAgentInput.model_validate(dict(rt.fold.calls[spawned.data.call_id].data.input))

    async def body() -> None:
        ended = await _outcome(scope, rt, spawned, child, prompt.prompt)
        # ponytail: a busy background child is left unfinished for the next run to restart.
        if isinstance(ended, HaltFailed):
            return
        if isinstance(ended, Parked):
            await stops.park(rt, spawned, ended)
            return
        data, text = ended
        late = await result_draft(rt, spawned.data.call_id, text, As("executed"))
        fields = {k: v for k, v in late.data.items() if k != "origin"}
        fields["is_error"] = data["status"] != "completed"
        await rt.append(draft("agent_finished", data), draft("tool_result_late", fields))

    tasks[spawned.data.child_thread_id] = asyncio.create_task(body())


async def _outcome[D](
    scope: Scope[D], rt: Runtime, spawned: AgentSpawnedEvent, child: Definition[None], prompt: str
) -> Ended | Parked | HaltFailed:
    """The child's terminal record, or its park. Under the parent's barrier the child is barred
    before it runs on, and one whose thread was never created is recorded cancelled."""
    barrier = open_cancel(rt.events)
    if barrier is not None and not await stops.bar(scope, spawned, barrier):
        return await stops.never_started(rt, spawned)
    return await _run(scope, rt, spawned, child, prompt)


async def once[D](scope: Scope[D], rt: Runtime, spawned: AgentSpawnedEvent) -> RunResult[str]:
    """The child run once more with what it already has: it appends nothing it holds."""
    child = _child(scope, spawned.data.agent_name)
    if child is None:
        raise AssertionError("a parked child is a configured subagent")
    inputs = len(_continues(rt, spawned.data.call_id)) + 1
    return await scope.execute(child, "", _launch(scope, rt, spawned, inputs))


async def _run[D](
    scope: Scope[D], rt: Runtime, spawned: AgentSpawnedEvent, child: Definition[None], prompt: str
) -> Ended | Parked | HaltFailed:
    """The child to its terminal result, or its park; subagent_stop may send it on, at most
    MAX_STOP_CONTINUES times, counted from the parent's log. A cancel is final: a cancelled
    child, or one whose parent is under a barrier, is never sent on."""
    call_id = spawned.data.call_id
    while True:
        reasons = _continues(rt, call_id)
        text = reasons[-1] if reasons else prompt
        result = await scope.execute(child, text, _launch(scope, rt, spawned, len(reasons) + 1))
        if isinstance(result, Parked):
            return result
        halt = busy(result)
        if halt is not None:
            return halt
        data, output = await finished(scope.sq, spawned.data.child_thread_id, result)
        data["output_ref"] = await text_ref(rt, output)
        if not rt.hooks.has("subagent_stop") or len(reasons) >= MAX_STOP_CONTINUES:
            return data, shown(str(data["status"]), output)
        ran = await rt.hooks.run("subagent_stop", STOP, AgentFinishedData.model_validate(data))
        ids = {"call_id": call_id}
        drafts = [
            decision_draft("subagent_stop", r, verdict(r, "stop"), said(r, "reason"), **ids)
            for r in ran
        ]
        await rt.append(*drafts)
        # A continue under a cancel is recorded and has no effect. The append (or its
        # refusal's reload) brought the parent's log up to date.
        barred = data["status"] == "cancelled" or open_cancel(rt.events) is not None
        if barred or all(verdict(r, "stop") != "continue" for r in ran):
            return data, shown(str(data["status"]), output)


def busy(result: RunResult[str]) -> HaltFailed | None:
    """A child whose lease another process holds (or took) is not finished: the parent halts
    branch_busy and a later run collects it."""
    if isinstance(result, Failed) and result.error.code == "branch_busy":
        return HaltFailed("branch_busy", result.error.message)
    return None


def _continues(rt: Runtime, call_id: str) -> list[str]:
    """The reasons subagent_stop sent this child on with, in order."""
    return [
        e.data.reason if isinstance(e.data.reason, str) else ""
        for e in rt.events
        if isinstance(e, HookDecisionEvent)
        and e.data.hook == "subagent_stop"
        and e.data.decision == "continue"
        and e.data.call_id == call_id
    ]


def _launch[D](scope: Scope[D], rt: Runtime, spawned: AgentSpawnedEvent, inputs: int) -> Launch:
    fold = rt.fold
    thread_id = fold.thread_id
    if thread_id is None:
        raise AssertionError("an acquired branch has a thread")
    parent: dict[str, JsonValue] = {
        "thread_id": thread_id,
        "branch_id": rt.writer.branch_id,
        "event_id": spawned.event_id,
        "relation": "subagent",
    }
    own = permissions(fold).model_copy(update={"mode": fold.mode})
    shared = scope.shared if spawned.data.isolation == "shared_sandbox" else None
    return Launch(
        spawned.data.child_thread_id,
        parent,
        "parent_agent",
        scope.principal,
        inputs,
        tuple(inherited(thread_id, fold, rt.budgets)),
        (own, *scope.ceilings),
        shared,
        (),
        Team(scope.lead(rt), spawned.data.agent_name),
    )
