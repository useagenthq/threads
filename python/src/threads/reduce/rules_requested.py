"""Operator controls that land only between turns (semantic rules 29 and 30 in
spec/schema/README.md): an output style is the pinned text, and a requested compaction is
answered by exactly one outcome that names it."""

from collections.abc import Mapping

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    CompactedEvent,
    CompactionFailedEvent,
    CompactionRequestedEvent,
    Event,
    EventId,
    InjectedEvent,
    ParseError,
)
from threads.reduce.fold import Fold, policy, reject
from threads.reduce.handlers import Handler, on


def cause_error(fold: Fold, event: Event, cause: EventId | MISSING) -> ParseError | None:
    """While a request is unanswered every compaction step names it; a named request is always
    the unanswered one."""
    open_id = None if fold.compaction_request is None else fold.compaction_request.event_id
    named = None if cause is MISSING else cause
    if named == open_id:
        return None
    if open_id is None:
        return reject(event, f"cause_event_id {named} names no unanswered compaction request")
    return reject(event, f"a compaction step while {open_id} is unanswered must name it")


def compacted_error(fold: Fold, event: CompactedEvent) -> ParseError | None:
    """A compacted answering a request covers exactly the request's range."""
    error = cause_error(fold, event, event.data.cause_event_id)
    request = fold.compaction_request
    if error is not None or request is None:
        return error
    data = event.data
    if data.trigger != "manual":
        return reject(event, "a compacted answering a request has trigger manual")
    start = None if fold.first_input is None else fold.first_input.seq
    if (data.from_seq, data.to_seq) != (start, request.seq - 1):
        return reject(event, f"a requested compaction covers {start}..{request.seq - 1}")
    side = data.summary_request_event_id
    if side is MISSING or fold.causes.get(side) != request.event_id:
        return reject(event, "the summary of a requested compaction names the request too")
    return None


def answer(fold: Fold, cause: EventId | MISSING) -> None:
    """An outcome naming the unanswered request answers it (cause_error checked the name)."""
    if fold.compaction_request is not None and cause == fold.compaction_request.event_id:
        fold.compaction_request = None


def _requested(fold: Fold, event: CompactionRequestedEvent) -> ParseError | None:
    if fold.compaction_request is not None:
        return reject(event, "a compaction is already requested")
    if fold.in_turn:
        return reject(event, "compaction_requested while a turn is open")
    if fold.first_input is None:
        return reject(event, "compaction_requested with nothing to compact yet")
    fold.compaction_request = event
    return None


def _failed(fold: Fold, event: CompactionFailedEvent) -> ParseError | None:
    error = cause_error(fold, event, event.data.cause_event_id)
    if error is None:
        answer(fold, event.data.cause_event_id)
    return error


def _style(fold: Fold, event: InjectedEvent) -> ParseError | None:
    """Rule 29: the pinned text, set by an operator between turns or restored by the host."""
    data = event.data
    pinned = policy(fold)
    styles = None if pinned is None or pinned.output_styles is MISSING else pinned.output_styles
    if data.trust != "trusted_instruction" or data.text is MISSING:
        return reject(event, "an output style is inline trusted_instruction text")
    if styles is None or styles.get(data.origin.id) != data.text:
        return reject(event, f"output style {data.origin.id} is not the pinned text")
    actor = event.actor
    if actor.kind == "host":
        return None
    if actor.kind != "user" or actor.principal is MISSING:
        return reject(event, "an output style is set by an operator or the host")
    return reject(event, "an output style set while a turn is open") if fold.in_turn else None


HANDLERS: Mapping[type, Handler] = dict(
    [
        on(CompactionRequestedEvent, _requested),
        on(CompactionFailedEvent, _failed),
        on(
            InjectedEvent,
            lambda fold, e: _style(fold, e) if e.data.source == "output_style" else None,
        ),
    ]
)
