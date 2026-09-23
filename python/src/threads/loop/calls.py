"""Tool calls from a response to their result: record, authorize, then run or settle.

A call that fails before `effect_begin` (unknown tool, bad arguments, a denial) gets an error
`tool_result` and nothing runs. Only an allowed call reaches the effect path.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final, Literal

from threads.log import AgentSpawnedEvent, CallId, EventId, ToolSpec, ToolUsePart
from threads.log.digest import canonical_sha256
from threads.loop import effects, gates, output, todos, tool_gates
from threads.loop.drafts import ActorKind, draft
from threads.loop.history import CallState, call_state
from threads.loop.results import As, reference_drafts, result_draft
from threads.loop.runtime import Failed, Halt, Parked, Runtime, fence, lost
from threads.loop.tools import NotSent, Output, Uncertain
from threads.result import Err, Ok
from threads.store import Draft
from threads.store.lines import uuid7

if TYPE_CHECKING:
    from pydantic import JsonValue

APPROVAL_TTL_MS: Final = 3_600_000
"""An approval challenge's default expiry."""


def call_drafts(rt: Runtime, request_id: EventId, uses: Sequence[ToolUsePart]) -> list[Draft]:
    """A `tool_call` per part; a call that can't run gets its error result in the same batch."""
    out: list[Draft] = []
    for use in uses:
        data: dict[str, JsonValue] = {
            "call_id": use.call_id,
            "name": use.name,
            "input": dict(use.input),
            "request_event_id": request_id,
        }
        out.append(draft("tool_call", data))
        why = _invalid(rt, use)
        if why is not None:
            error: dict[str, JsonValue] = {
                "call_id": use.call_id,
                "completeness": "complete",
                "is_error": True,
                "origin": "not_executed",
                "preview": why,
            }
            out.append(draft("tool_result", error))
    return out


def _invalid(rt: Runtime, use: ToolUsePart) -> str | None:
    spec = rt.fold.tools.get(use.name)
    if spec is None:
        return f"unknown tool {use.name}"
    if spec.defer_loading is True:
        return f"tool_not_loaded: search for {use.name} with tool_search first"
    if output.is_candidate(rt.fold, spec):
        return None  # validated against the pinned output schema, recorded as output_validated
    return rt.tools.invalid(spec, use.input)


async def run_call(rt: Runtime, call_id: CallId) -> Halt | None:
    """Advances one pending call until it has a result, or the run must stop."""
    while call_id in rt.fold.pending:
        state = call_state(rt.events, call_id)
        spec = rt.fold.tools[state.call.data.name]
        halt = await _advance(rt, state, spec)
        if halt is not None:
            return halt
    return None


async def _advance(rt: Runtime, state: CallState, spec: ToolSpec) -> Halt | None:
    call_id = state.call.data.call_id
    if state.decision is None:
        return await _authorize(rt, state, spec)
    if state.decision == "deny" or state.approved is False:
        return await close(rt, call_id, "denied", "denied by policy")
    if state.decision == "ask" and state.approved is None:
        return await await_approval(rt, state, "host")
    return await _run(rt, state, spec)


async def _run(rt: Runtime, state: CallState, spec: ToolSpec) -> Halt | None:
    """An authorized call: framework tools change only the log; the rest dispatch."""
    if output.is_candidate(rt.fold, spec):
        return await output.validate(rt, state)
    if spec.name == "todo_write":
        return await todos.write(rt, state)
    if rt.framework is not None and spec.name in rt.framework.names:
        return await rt.framework.run(rt, state)
    if spec.effect_class == "read_only":
        return await _read_only(rt, state, spec)
    return await _effect(rt, state, spec)


async def _effect(rt: Runtime, state: CallState, spec: ToolSpec) -> Halt | None:
    match state.effect:
        case None | "safe_to_retry" | "not_sent" | "assume_not_done":
            return await effects.dispatch(rt, state, spec)
        case "unknown":
            return await effects.settle(rt, state, spec, state.unknown_reason or "", "host")
        case "begun":
            raise AssertionError("an effect begun in this run is settled before the next step")
        case _:
            return await effects.close_settled(rt, state, "host")


async def close(
    rt: Runtime,
    call_id: CallId,
    origin: Literal["denied", "not_executed"],
    why: str,
    actor: ActorKind = "host",
) -> Halt | None:
    """Closes a call that never ran with an error result."""
    result = await result_draft(rt, call_id, why, As(origin, True, actor))
    done = await rt.append(result)
    return lost(done.error) if isinstance(done, Err) else None


async def recheck(rt: Runtime, state: CallState) -> Halt | None:
    """An allow recorded before a crash is re-checked against current policy before dispatch; a
    changed decision is recorded and followed instead."""
    spec = rt.fold.tools[state.call.data.name]
    if rt.authorize(rt.fold, state.call.data, spec).decision == "allow":
        return None
    return await _authorize(rt, state, spec, "recovery")


async def _authorize(
    rt: Runtime, state: CallState, spec: ToolSpec, actor: ActorKind = "host"
) -> Halt | None:
    policy = rt.authorize(rt.fold, state.call.data, spec)
    decision, hooked = await tool_gates.authorize(rt, state.call, policy)
    data: dict[str, JsonValue] = {
        "call_id": state.call.data.call_id,
        "decision": decision.decision,
        "source": decision.source,
        "mode": rt.fold.mode,
    }
    if decision.rule is not None:
        data["rule_id"] = decision.rule
    drafts = [*hooked, draft("permission_decision", data, actor)]
    if decision.decision == "ask":
        drafts.append(_challenge(rt, state, actor))
    done = await rt.append(*drafts)
    if isinstance(done, Err):
        return lost(done.error)
    if decision.decision == "deny":
        ids = {"call_id": state.call.data.call_id}
        return await gates.observe(rt, "permission_denied", state.call.data, **ids)
    return None


def _challenge(rt: Runtime, state: CallState, actor: ActorKind) -> Draft:
    args = canonical_sha256(dict(state.call.data.input))
    if not isinstance(args, Ok):
        raise AssertionError("a parsed call input always canonicalizes")
    now = rt.clock()
    data: dict[str, JsonValue] = {
        "challenge_id": uuid7(now),
        "call_id": state.call.data.call_id,
        "args_hash": args.value,
        "expires_at": now + APPROVAL_TTL_MS,
    }
    return draft("approval_requested", data, actor)


async def await_approval(rt: Runtime, state: CallState, actor: ActorKind) -> Halt | None:
    """An open challenge parks the run; an expired one counts as a denial."""
    challenge = state.challenge
    if challenge is None:
        raise AssertionError("an ask decision is recorded with its challenge")
    if challenge.data.expires_at <= rt.clock():
        return await close(rt, state.call.data.call_id, "denied", "approval expired", actor)
    address: dict[str, JsonValue] = {"kind": "approval", "id": challenge.data.challenge_id}
    if not any(a.kind == "approval" and a.id == address["id"] for a in rt.fold.parked):
        data: dict[str, JsonValue] = {
            "address": address,
            "reason": "awaiting_approval",
            "expires_at": challenge.data.expires_at,
        }
        done = await rt.append(draft("parked", data, actor))
        if isinstance(done, Err):
            return lost(done.error)
    return Parked("awaiting_approval", tuple(rt.fold.parked))


async def _read_only(rt: Runtime, state: CallState, spec: ToolSpec) -> Halt | None:
    """A read_only call writes no effect events: it changes nothing, so it simply runs."""
    inv = effects.invocation(state, spec)
    stale = await fence(rt)
    if stale is not None:
        return stale
    references: list[Draft] = []
    match await rt.tools.dispatch(inv):
        case Output(
            text=text, is_error=is_error, full_output=full, references=recalled, content=parts
        ):
            how = As("executed", is_error)
            result = await result_draft(rt, inv.call_id, text, how, full, content=parts)
            references = reference_drafts(recalled)
        case Uncertain(reason=reason):
            text = f"{reason}: the read did not finish"
            result = await result_draft(rt, inv.call_id, text, As("executed", True))
        case NotSent(unmatched=True):
            return Failed("unmatched_external_op", f"no recorded stub for {spec.name}")
        case NotSent():
            result = await result_draft(rt, inv.call_id, "not sent", As("not_executed", True))
    done = await rt.append(result, *references)
    return lost(done.error) if isinstance(done, Err) else None


async def cancel_call(rt: Runtime, call_id: CallId, actor: ActorKind = "host") -> Halt | None:
    """Closes a pending call behind a cancel barrier: one that never began as not_executed; one
    whose effect may have been sent is settled or parked, never assumed undone."""
    state = call_state(rt.events, call_id)
    spec = rt.fold.tools[state.call.data.name]
    if rt.framework is not None and _child_started(rt, call_id):
        # A spawned child is never cancelled over: the parent runs it (barred) to its end and
        # records its one agent_finished, or parks on it (spec/schema/README.md).
        return await rt.framework.run(rt, state)
    match state.effect:
        case "begun" | "unknown":
            return await effects.settle(rt, state, spec, state.unknown_reason or "", actor)
        case None | "safe_to_retry" | "not_sent" | "assume_not_done":
            return await close(rt, call_id, "not_executed", "not executed: cancelled", actor)
        case _:
            return await effects.close_settled(rt, state, actor)


def _child_started(rt: Runtime, call_id: CallId) -> bool:
    return any(isinstance(e, AgentSpawnedEvent) and e.data.call_id == call_id for e in rt.events)
