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

from threads.log import EventId, ModelRequestEvent
from threads.loop import calls, effects
from threads.loop.attempt import response_drafts
from threads.loop.drafts import draft
from threads.loop.history import CallState, call_state
from threads.loop.model import Found, NotFound
from threads.loop.runtime import Halt, Runtime, lost
from threads.result import Err

if TYPE_CHECKING:
    from pydantic import JsonValue


async def recover(rt: Runtime) -> Halt | None:
    """Decides every in-doubt item; returns the first halt (a park or a failure), if any. A torn
    import's `log_repaired` comes before this, when the store hands the branch over."""
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


async def _model(rt: Runtime, request_id: EventId) -> Halt | None:
    """a final `found` is recorded without a new call; a final `not_found` is
    not_sent; anything else is unknown, and the loop's crash re-send budget decides."""
    outcome = "unknown"
    if rt.model.info.lookup != "none":
        match await rt.model.lookup(f"{rt.writer.branch_id}:{request_id}"):
            case Found(value=response, provider_request_id=provider_id):
                found = response_drafts(rt, request_id, response, provider_id or str(request_id))
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
        case "committed" | "confirmed_success":
            return await effects.materialize(rt, state, "recovery")
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
        case _:
            # Settled safe to retry or not sent: the loop re-dispatches under the same key.
            return None


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
