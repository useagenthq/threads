"""Tool calls from a response to their result: record, authorize, then run or settle.

A call that fails before `effect_begin` (unknown tool, bad arguments, a denial) gets an error
`tool_result` and nothing runs. Only an allowed call reaches the effect path.
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Final, Literal

from pydantic import JsonValue

from threads.log import AgentSpawnedEvent, CallId, ToolSpec
from threads.log.digest import canonical_sha256
from threads.loop import effects, gates, output, questions, search, todos, tool_gates
from threads.loop.drafts import ActorKind, draft
from threads.loop.history import CallState, call_state, open_cancel
from threads.loop.results import As, reference_drafts, result_draft
from threads.loop.runtime import Failed, Halt, Parked, Runtime, fence, lost
from threads.loop.tools import Dispatched, Invocation, NotSent, Output, Uncertain
from threads.reduce.fold import call_spec
from threads.result import Err, Ok
from threads.store.lines import uuid7

if TYPE_CHECKING:
    from threads.store import Draft

APPROVAL_TTL_MS: Final = 3_600_000
"""An approval challenge's default expiry."""


def pending_spec(rt: Runtime, call_id: CallId) -> ToolSpec:
    """The call-time spec of a call the loop runs. Recording closes a call to an unknown tool
    pre-effect, and recovery closes or parks one it finds, so every call the loop runs has one."""
    spec = call_spec(rt.fold, call_id)
    if spec is None:
        raise AssertionError(f"call {call_id} runs with no call-time spec")
    return spec


async def run_call(rt: Runtime, call_id: CallId) -> Halt | None:
    """Advances one pending call until it has a result, or the run must stop. A cancel that
    lands meanwhile (authorize's hooks await) stops it: the cancellation step closes the call."""
    while call_id in rt.fold.pending:
        if open_cancel(rt.events) is not None:
            return None
        state = call_state(rt.events, call_id)
        halt = await _advance(rt, state, pending_spec(rt, call_id))
        if halt is not None:
            return halt
    return None


async def _advance(rt: Runtime, state: CallState, spec: ToolSpec) -> Halt | None:
    call_id = state.call.data.call_id
    if state.decision is None:
        return await authorize(rt, state, spec)
    if state.decision == "deny" or state.approved is False:
        return await close(rt, call_id, "denied", denial(state))
    if state.decision == "ask" and state.approved is None:
        return await await_approval(rt, state, "host")
    return await _run(rt, state, spec)


async def _run(rt: Runtime, state: CallState, spec: ToolSpec) -> Halt | None:
    """An authorized call: framework tools change only the log; the rest dispatch."""
    if output.is_candidate(rt.fold, spec):
        return await output.validate(rt, state)
    if search.is_search(rt.fold, spec):
        return await search.run(rt, state)
    if spec.name in _LOG_TOOLS:
        return await _LOG_TOOLS[spec.name](rt, state)
    if rt.framework is not None and spec.name in rt.framework.names:
        return await rt.framework.run(rt, state)
    if spec.effect_class == "read_only":
        return await _read_only(rt, state, spec)
    return await _effect(rt, state, spec)


_LOG_TOOLS: Final[Mapping[str, Callable[[Runtime, CallState], Awaitable[Halt | None]]]] = {
    "todo_write": todos.write,
    "ask_user": questions.ask,
}
"""The framework tools that only append to this thread's log, by name."""


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


async def recheck(rt: Runtime, state: CallState, spec: ToolSpec) -> Halt | None:
    """An allow recorded before a crash is re-checked against current policy before dispatch; a
    changed decision is recorded and followed instead."""
    if rt.authorize(rt.fold, state.call.data, spec).decision == "allow":
        return None
    return await authorize(rt, state, spec, "recovery")


async def authorize(
    rt: Runtime, state: CallState, spec: ToolSpec, actor: ActorKind = "host"
) -> Halt | None:
    """Records the call's authorization: policy, then before_tool; an ask opens its challenge."""
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
    if decision.reason is not None:
        data["reason"] = decision.reason
    done = await rt.append(*hooked, draft("permission_decision", data, actor))
    if isinstance(done, Err):
        return lost(done.error)
    if decision.decision == "deny":
        ids = {"call_id": state.call.data.call_id}
        return await gates.observe(rt, "permission_denied", state.call.data, **ids)
    return None


def _challenge(rt: Runtime, state: CallState) -> dict[str, JsonValue]:
    args = canonical_sha256(dict(state.call.data.input))
    if not isinstance(args, Ok):
        raise AssertionError("a parsed call input always canonicalizes")
    now = rt.clock()
    return {
        "challenge_id": uuid7(now),
        "call_id": state.call.data.call_id,
        "args_hash": args.value,
        "expires_at": now + APPROVAL_TTL_MS,
    }


async def await_approval(rt: Runtime, state: CallState, actor: ActorKind) -> Halt | None:
    """An ask's turn to run: its challenge opens and the run parks on it; an expired challenge
    counts as a denial. The challenge is recorded here, not with the decision, so every call of
    the response is recorded and authorized before the first one asks."""
    drafts: list[Draft] = []
    if state.challenge is None:
        opened = _challenge(rt, state)
        drafts.append(draft("approval_requested", opened, actor))
        challenge_id, expires_at = opened["challenge_id"], opened["expires_at"]
    elif state.challenge.data.expires_at <= rt.clock():
        return await close(rt, state.call.data.call_id, "denied", "denied: approval expired", actor)
    else:
        challenge_id = state.challenge.data.challenge_id
        expires_at = state.challenge.data.expires_at
    if not any(a.kind == "approval" and a.id == challenge_id for a in rt.fold.parked):
        data: dict[str, JsonValue] = {
            "address": {"kind": "approval", "id": challenge_id},
            "reason": "awaiting_approval",
            "expires_at": expires_at,
        }
        drafts.append(draft("parked", data, actor))
    if drafts:
        done = await rt.append(*drafts)
        if isinstance(done, Err):
            return lost(done.error)
    return Parked("awaiting_approval", tuple(rt.fold.parked))


def denial(state: CallState) -> str:
    """A denied call's result, as the model sees it: `denied`, with the policy's or a hook's
    reason, or why its approval failed."""
    if state.approved is False:
        return "denied: approval denied"
    reason = state.decision_reason
    return "denied" if reason is None else f"denied: {reason}"


async def _read_only(rt: Runtime, state: CallState, spec: ToolSpec) -> Halt | None:
    """A read_only call writes no effect events: it changes nothing, so it simply runs."""
    inv = effects.invocation(state, spec)
    stale = await fence(rt)
    if stale is not None:
        return stale
    return await record_read(rt, inv, await rt.tools.dispatch(inv))


async def record_read(rt: Runtime, inv: Invocation, ran: Dispatched) -> Halt | None:
    """A read_only call's result and its references. An uncertain read is just a failed one."""
    references: list[Draft] = []
    match ran:
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
            return Failed("unmatched_external_op", f"no recorded stub for {inv.spec.name}")
        case NotSent():
            result = await result_draft(rt, inv.call_id, "not sent", As("not_executed", True))
    done = await rt.append(result, *references)
    return lost(done.error) if isinstance(done, Err) else None


async def cancel_call(rt: Runtime, call_id: CallId, actor: ActorKind = "host") -> Halt | None:
    """Closes a pending call behind a cancel barrier: one that never began as not_executed; one
    whose effect may have been sent is settled or parked, never assumed undone."""
    state = call_state(rt.events, call_id)
    spec = call_spec(rt.fold, call_id)
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
