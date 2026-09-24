"""Tool calls, approvals, effects, results, context edits, cancellation and parking (semantic
rules 7, 8, 9, 11, 13, 19, 25 in spec/schema/README.md)."""

from collections.abc import Callable, Mapping

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    ApprovalBinding,
    ApprovalDeniedEvent,
    ApprovalGrantedEvent,
    ApprovalRequestedEvent,
    CancelledEvent,
    CancelRequestedEvent,
    ContextEditedEvent,
    Edit,
    EffectBeginEvent,
    EffectCommitEvent,
    EffectResolvedEvent,
    EffectUnknownEvent,
    ParkAddress,
    ParkedEvent,
    ParseError,
    PermissionDecisionEvent,
    ResumedEvent,
    ToolCallEvent,
    ToolResultEvent,
    ToolResultLateEvent,
)
from threads.reduce.fold import EffectStatus, Fold, Result, reject
from threads.reduce.handlers import Handler, on
from threads.reduce.redaction import span_error, text_part

type EffectEvent = EffectBeginEvent | EffectCommitEvent | EffectUnknownEvent | EffectResolvedEvent


def _tool_call(fold: Fold, event: ToolCallEvent) -> ParseError | None:
    call_id = event.data.call_id
    if call_id in fold.calls:
        return reject(event, f"call_id {call_id} is already used on this branch")
    fold.calls[call_id] = event
    fold.pending.append(call_id)
    # The spec comes from the tool set in force at the call (schema README, derived values).
    spec = fold.tools.get(event.data.name)
    if spec is not None:
        fold.call_specs[call_id] = spec
    return None


def _permission(fold: Fold, event: PermissionDecisionEvent) -> None:
    if event.data.decision == "allow":
        fold.allowed.add(event.data.call_id)


def _approval_requested(fold: Fold, event: ApprovalRequestedEvent) -> None:
    fold.challenges[event.data.challenge_id] = (event.data.call_id, event.data.args_hash)


def _consume(
    fold: Fold, event: ApprovalGrantedEvent | ApprovalDeniedEvent, data: ApprovalBinding
) -> ParseError | None:
    if data.challenge_id in fold.consumed:
        return reject(event, f"challenge {data.challenge_id} is already consumed")
    if fold.challenges.get(data.challenge_id) != (data.call_id, data.args_hash):
        message = "the approval does not match the open challenge's call_id and args_hash"
        return reject(event, message, "approval_mismatch")
    fold.consumed.add(data.challenge_id)
    del fold.challenges[data.challenge_id]
    return None


def _granted(fold: Fold, event: ApprovalGrantedEvent) -> ParseError | None:
    error = _consume(fold, event, event.data)
    if error is None:
        fold.allowed.add(event.data.call_id)
    return error


def _denied(fold: Fold, event: ApprovalDeniedEvent) -> ParseError | None:
    return _consume(fold, event, event.data)


def _effect_error(fold: Fold, event: EffectEvent, call: ToolCallEvent) -> str | None:
    spec = fold.call_specs.get(call.data.call_id)
    if spec is not None and spec.effect_class == "read_only":
        return "read_only calls write no effect events"
    if not isinstance(event, EffectBeginEvent):
        return None
    if call.data.call_id not in fold.allowed:
        return "effect_begin without permission_decision allow or a consumed approval"
    if fold.last_cancel_seq > call.seq:
        return "effect_begin after a cancel_requested barrier"
    return None


def _effect(status: EffectStatus) -> Callable[[Fold, EffectEvent], ParseError | None]:
    def step(fold: Fold, event: EffectEvent) -> ParseError | None:
        call = fold.calls.get(event.data.call_id)
        if call is None:
            return reject(event, f"no tool_call {event.data.call_id}")
        error = _effect_error(fold, event, call)
        if error is not None:
            return reject(event, error)
        key = f"{call.branch_id}:{call.data.call_id}"
        fold.effects[key] = (call.data.call_id, status)
        return None

    return step


def _tool_result(fold: Fold, event: ToolResultEvent) -> ParseError | None:
    data = event.data
    if data.call_id not in fold.pending:
        return reject(event, f"no pending tool_call {data.call_id}")
    if data.origin == "answered" and ParkAddress(kind="input", id=data.call_id) not in fold.parked:
        return reject(event, "an answered result needs an open parked input address")
    fold.pending.remove(data.call_id)
    fold.results[data.call_id] = data
    if data.origin == "deferred":
        fold.deferred.add(data.call_id)
    return None


def _late(fold: Fold, event: ToolResultLateEvent) -> ParseError | None:
    if event.data.call_id not in fold.deferred:
        return reject(event, "tool_result_late needs an earlier deferred tool_result")
    fold.results[event.data.call_id] = event.data
    return None


def _context_edited(fold: Fold, event: ContextEditedEvent) -> ParseError | None:
    for edit in event.data.edits:
        result = fold.results.get(edit.call_id)
        if result is None:
            return reject(event, f"context_edited names {edit.call_id}, which has no result")
        error = _redaction_error(edit, result) if edit.action == "redact" else None
        if error is not None:
            return reject(event, error)
    return None


def _redaction_error(edit: Edit, result: Result) -> str | None:
    text = text_part(result, edit.part)
    if text is None:
        return "a redaction's part must be a text part of the result"
    return span_error(text, () if edit.spans is MISSING else edit.spans)


def _cancel_requested(fold: Fold, event: CancelRequestedEvent) -> None:
    fold.last_cancel_seq = event.seq
    fold.cancel_scopes[event.event_id] = event.data.scope


def _cancelled(fold: Fold, event: CancelledEvent) -> ParseError | None:
    if any(status in ("begun", "unknown") for _, status in fold.effects.values()):
        return reject(event, "cancelled while an effect is begun or unknown")
    if fold.cancel_scopes.get(event.data.request_event_id) in ("thread", "tree"):
        fold.cancelled = True
    return None


def _parked(fold: Fold, event: ParkedEvent) -> None:
    fold.parked.append(event.data.address)


def _resumed(fold: Fold, event: ResumedEvent) -> None:
    if event.data.address in fold.parked:
        fold.parked.remove(event.data.address)


HANDLERS: Mapping[type, Handler] = dict(
    [
        on(ToolCallEvent, _tool_call),
        on(PermissionDecisionEvent, _permission),
        on(ApprovalRequestedEvent, _approval_requested),
        on(ApprovalGrantedEvent, _granted),
        on(ApprovalDeniedEvent, _denied),
        on(EffectBeginEvent, _effect("begun")),
        on(EffectCommitEvent, _effect("committed")),
        on(EffectUnknownEvent, _effect("unknown")),
        on(EffectResolvedEvent, _effect("resolved")),
        on(ToolResultEvent, _tool_result),
        on(ToolResultLateEvent, _late),
        on(ContextEditedEvent, _context_edited),
        on(CancelRequestedEvent, _cancel_requested),
        on(CancelledEvent, _cancelled),
        on(ParkedEvent, _parked),
        on(ResumedEvent, _resumed),
    ]
)
