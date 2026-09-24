"""Semantic recovery on acquire: before anything new runs, every in-doubt item
the crash left is decided and the decision appended durably.

- a `model_request` without a response: lookup first, else abandoned as unknown;
- an effect begun or unknown: `effect_unknown`, then the class rule settles it or it parks;
- an effect committed without its result: the result is materialized, never re-run;
- a call that never began: cancellation, denial and pending approval are honoured, and an allow
  is re-checked against current policy before the loop may dispatch it.

Recovery is idempotent: a second pass over its own output finds nothing to decide.
"""

from typing import TYPE_CHECKING, assert_never

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    CancelledEvent,
    EventId,
    ModelRequestEvent,
    ResumedEvent,
    SteerEvent,
    ToolSpec,
)
from threads.loop import calls, effects, record
from threads.loop.attempt import response_draft
from threads.loop.drafts import draft
from threads.loop.history import CallState, call_state, open_cancel, turn_events
from threads.loop.manual import settle_requested
from threads.loop.model import (
    Found,
    LooksUp,
    LookupUnknown,
    NotFound,
    NotFoundNonfinal,
    looked_up,
)
from threads.loop.runtime import Failed, Halt, Runtime, WriterContext, epoch_model, fence, lost
from threads.reduce.fold import call_spec
from threads.reduce.openers import turn_start
from threads.result import Err, Ok

if TYPE_CHECKING:
    from pydantic import JsonValue


async def recover(rt: Runtime) -> Halt | None:
    """Decides every in-doubt item; returns the first halt (a park or a failure), if any. A torn
    import's `log_repaired` comes before this, when the store hands the branch over. An open turn
    with nothing pending is closed or left to continue (spec/schema/README.md wire rule 11)."""
    if rt.fold.in_turn and any(isinstance(e, CancelledEvent) for e in turn_events(rt.events)):
        # An older writer recorded `cancelled` without its turn_completed: it ends cancelled.
        done = await rt.append(draft("turn_completed", {"reason": "cancelled"}, "recovery"))
        return lost(done.error) if isinstance(done, Err) else None
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
    return await settle_requested(rt)


def _open_turn_to_close(rt: Runtime) -> bool:
    """An open turn with nothing pending closes as interrupted, unless none of its input was sent
    yet (no model_request after its last user_input or steer), a cancel is durable in it, or a
    question was answered (or expired) since its last model_request: the run then continues it
    and sends the answer on, or carries the cancel out."""
    fold = rt.fold
    if not fold.in_turn or fold.pending or fold.open_requests or fold.parked:
        return False
    if record.owed(rt):
        return False  # the response's calls are recorded next, never left without results
    if open_cancel(rt.events) is not None or _answered_since_request(rt):
        return False
    # The turn's opener (an input, a woken, a receipt) or a later steer.
    opened = turn_start(rt.events) or 0
    steers = [i for i, e in enumerate(rt.events) if isinstance(e, SteerEvent) and i > opened]
    start = steers[-1] if steers else opened
    # A requested compaction's side request is cut before the input, so it never sent it.
    return any(
        isinstance(e, ModelRequestEvent) and e.data.cause_event_id is MISSING
        for e in rt.events[start:]
    )


async def _model(rt: Runtime, request_id: EventId) -> Halt | None:
    """a final `found` is recorded without a new call; a final `not_found` is
    not_sent; anything else is unknown, and the loop's crash re-send budget decides."""
    outcome = "unknown"
    # No settings change can land while a request is open: the current epoch is the request's.
    model = epoch_model(rt)
    # A model without the method is treated as declaring none: check() and the first run
    # refuse one that declares a lookup, and recovery only runs inside a run.
    if model is not None and model.info.lookup != "none" and isinstance(model, LooksUp):
        stale = await fence(rt)
        if stale is not None:
            return stale
        looked = await looked_up(
            model.lookup(f"{rt.writer.branch_id}:{request_id}", WriterContext(rt)), Ok
        )
        if isinstance(looked, Err):
            # The fence refused at the lookup's real send point: this writer lost its lease.
            return Failed("branch_busy", looked.error.message)
        match looked.value:
            case Found(value=response) if response.provider_request_id is not None:
                # A found response without the provider's id can't be recorded: it stays unknown.
                found = response_draft(request_id, response, response.provider_request_id)
                done = await rt.append(found)
                return lost(done.error) if isinstance(done, Err) else None
            case NotFound():
                outcome = "not_sent"
            case Found() | NotFoundNonfinal() | LookupUnknown():
                pass  # no final answer this log can record: the attempt stays unknown
            case _:
                assert_never(looked.value)
    data: dict[str, JsonValue] = {
        "request_event_id": request_id,
        "provider_outcome": outcome,
        "reason": "crash",
    }
    done = await rt.append(draft("model_attempt_abandoned", data, "recovery"))
    return lost(done.error) if isinstance(done, Err) else None


async def _call(rt: Runtime, state: CallState) -> Halt | None:
    spec = call_spec(rt.fold, state.call.data.call_id)
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
            return await _close_gone_or_dispatch(rt, state, spec, began=False)
        case "safe_to_retry" | "not_sent" | "assume_not_done":
            # Settled as never performed: the loop may re-dispatch under the same key, after
            # the same cancellation and policy re-checks as a call that never began.
            return await _close_gone_or_dispatch(rt, state, spec, began=True)
        case _:
            return await effects.close_settled(rt, state, "recovery")


async def _close_gone_or_dispatch(
    rt: Runtime, state: CallState, spec: ToolSpec | None, *, began: bool
) -> Halt | None:
    """A call the loop would dispatch never runs without a tool: one made to a tool not in the
    set closes not_executed, and so does one that never began whose tool a later tools_changed
    removed (a removal is a policy change, so an earlier allow never dispatches past it)."""
    call_id = state.call.data.call_id
    if spec is None:
        why = f"not executed: unknown tool {state.call.data.name}"
        return await calls.close(rt, call_id, "not_executed", why, "recovery")
    if not began and spec.name not in rt.fold.tools:
        why = f"not executed: {spec.name} was removed from the tool set"
        return await calls.close(rt, call_id, "not_executed", why, "recovery")
    return await _never_began(rt, state, spec)


async def _never_began(rt: Runtime, state: CallState, spec: ToolSpec) -> Halt | None:
    """Not started is not permission."""
    call_id = state.call.data.call_id
    if rt.fold.last_cancel_seq > state.call.seq:
        return await calls.cancel_call(rt, call_id, "recovery")
    if state.approved is False:
        return await calls.close(rt, call_id, "denied", calls.denial(state), "recovery")
    if state.decision == "ask" and state.approved is None and state.challenge is not None:
        return await calls.await_approval(rt, state, "recovery")
    if state.decision == "allow":
        return await calls.recheck(rt, state, spec)
    return None


def _answered_since_request(rt: Runtime) -> bool:
    """A resumed of a question after the turn's last model_request: its answer is still to send."""
    for event in reversed(rt.events):
        if isinstance(event, ModelRequestEvent):
            return False
        if isinstance(event, ResumedEvent) and event.data.address.kind == "input":
            return True
    return False
