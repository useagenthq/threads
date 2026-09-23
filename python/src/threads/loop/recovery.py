"""Semantic recovery on acquire: before anything new runs, every in-doubt item
the crash left is decided and the decision appended durably.

- a `model_request` without a response: lookup first, else abandoned as unknown;
- an effect begun or unknown: `effect_unknown`, then the class rule settles it or it parks;
- an effect committed without its result: the result is materialized, never re-run;
- a call that never began: cancellation, denial and pending approval are honoured, and an allow
  is re-checked against current policy before the loop may dispatch it.

Recovery is idempotent: a second pass over its own output finds nothing to decide.
"""

from typing import TYPE_CHECKING

from threads.log import EventId, ModelRequestEvent, SteerEvent, UserInputEvent
from threads.loop import calls, effects
from threads.loop.attempt import response_drafts
from threads.loop.drafts import draft
from threads.loop.history import CallState, call_state
from threads.loop.model import Found, NotFound
from threads.loop.runtime import Halt, Runtime, WriterContext, fence, lost
from threads.result import Err

if TYPE_CHECKING:
    from pydantic import JsonValue


async def recover(rt: Runtime) -> Halt | None:
    """Decides every in-doubt item; returns the first halt (a park or a failure), if any. A torn
    import's `log_repaired` comes before this, when the store hands the branch over. An open turn
    with nothing pending is closed or left to continue (spec/schema/README.md wire rule 11)."""
    if _open_turn_to_close(rt):
        # The turn's input was sent but nothing is pending: how it would have ended is lost.
        done = await rt.append(draft("turn_completed", {"reason": "interrupted"}, "recovery"))
        return lost(done.error) if isinstance(done, Err) else None
    requests = [e for e in rt.events if isinstance(e, ModelRequestEvent)]
    for request in requests:
        if request.event_id in rt.fold.open_requests:
            halt = await _model(rt, request.event_id)
            if halt is not None:
                return halt
    for call_id in list(rt.fold.pending):
        halt = await _call(rt, call_state(rt.events, call_id))
        if halt is not None:
            return halt
    return None


def _open_turn_to_close(rt: Runtime) -> bool:
    """An open turn with nothing pending closes as interrupted, unless none of its input was sent
    yet (no model_request after its last user_input or steer): the run then continues it."""
    fold = rt.fold
    if not fold.in_turn or fold.pending or fold.open_requests or fold.parked:
        return False
    inputs = [i for i, e in enumerate(rt.events) if isinstance(e, UserInputEvent | SteerEvent)]
    start = inputs[-1] if inputs else 0
    return any(isinstance(e, ModelRequestEvent) for e in rt.events[start:])


async def _model(rt: Runtime, request_id: EventId) -> Halt | None:
    """a final `found` is recorded without a new call; a final `not_found` is
    not_sent; anything else is unknown, and the loop's crash re-send budget decides."""
    outcome = "unknown"
    if rt.model.info.lookup != "none":
        stale = await fence(rt)
        if stale is not None:
            return stale
        match await rt.model.lookup(f"{rt.writer.branch_id}:{request_id}", WriterContext(rt)):
            case Found(value=response) if response.provider_request_id is not None:
                # A found response without the provider's id can't be recorded: it stays unknown.
                found = response_drafts(rt, request_id, response, response.provider_request_id)
                done = await rt.append(*found)
                return lost(done.error) if isinstance(done, Err) else None
            case NotFound():
                outcome = "not_sent"
            case _:
                pass
    data: dict[str, JsonValue] = {
        "request_event_id": request_id,
        "provider_outcome": outcome,
        "reason": "crash",
    }
    done = await rt.append(draft("model_attempt_abandoned", data, "recovery"))
    return lost(done.error) if isinstance(done, Err) else None


async def _call(rt: Runtime, state: CallState) -> Halt | None:
    spec = rt.fold.tools[state.call.data.name]
    match state.effect:
        case "begun":
            data = {"call_id": state.call.data.call_id, "reason": "crash_after_begin"}
            done = await rt.append(draft("effect_unknown", data, "recovery"))
            if isinstance(done, Err):
                return lost(done.error)
            state = call_state(rt.events, state.call.data.call_id)
            return await effects.settle(rt, state, spec, "crash_after_begin", "recovery")
        case "unknown":
            reason = state.unknown_reason or "crash_after_begin"
            return await effects.settle(rt, state, spec, reason, "recovery")
        case None:
            return await _never_began(rt, state)
        case "safe_to_retry" | "not_sent" | "assume_not_done":
            # Settled as never performed: the loop may re-dispatch under the same key, after
            # the same cancellation and policy re-checks as a call that never began.
            return await _never_began(rt, state)
        case _:
            return await effects.close_settled(rt, state, "recovery")


async def _never_began(rt: Runtime, state: CallState) -> Halt | None:
    """Not started is not permission."""
    call_id = state.call.data.call_id
    if rt.fold.last_cancel_seq > state.call.seq:
        return await calls.cancel_call(rt, call_id, "recovery")
    if state.decision == "deny" or state.approved is False:
        return await calls.close(rt, call_id, "denied", "denied by policy", "recovery")
    if state.decision == "ask" and state.approved is None:
        return await calls.await_approval(rt, state, "recovery")
    if state.decision == "allow":
        return await calls.recheck(rt, state)
    return None
