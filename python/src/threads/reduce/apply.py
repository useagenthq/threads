"""`validate_next` plus the fold step: one event against the state of everything before it."""

from collections.abc import Mapping

from threads.log import Event, Header, ParseError, UnknownEvent
from threads.reduce import (
    rules_loaded,
    rules_misc,
    rules_phase2,
    rules_remote,
    rules_requested,
    rules_team,
    rules_tools,
    rules_turns,
    rules_wake,
    team_fold,
)
from threads.reduce.fold import Fold, reject
from threads.reduce.handlers import Handler

_HANDLERS: Mapping[type, Handler] = {
    **rules_turns.HANDLERS,
    **rules_tools.HANDLERS,
    **rules_misc.HANDLERS,
    **rules_requested.HANDLERS,
    **rules_wake.HANDLERS,
    **rules_loaded.HANDLERS,
    **rules_remote.HANDLERS,
}


def enter_segment(fold: Fold, header: Header) -> None:
    """Starts a segment: its events must carry the header's branch_id (semantic rule 4)."""
    if fold.thread_id is None:
        fold.thread_id = header.thread_id
    fold.segment = header.branch_id


def apply(fold: Fold, event: Event | UnknownEvent) -> ParseError | None:
    """Checks the envelope and semantic rules for the next event of the resolved chain, then
    folds it in. On an error the fold is unchanged. `seq` contiguity and the hash chain are
    checked by the reader of the bytes, before this."""
    error = _envelope_error(fold, event) or rules_remote.follows_call(fold, event)
    if error is None and not isinstance(event, UnknownEvent):
        handler = _HANDLERS.get(type(event))
        error = (
            rules_phase2.not_yet(event)
            or rules_team.check(fold, event)
            or (None if handler is None else handler(fold, event))
        )
    if error is not None:
        return error
    fold.seq = event.seq
    fold.epoch = event.epoch
    fold.event_ids.add(event.event_id)
    if not isinstance(event, UnknownEvent):
        fold.events.append(event)
        team_fold.advance(fold, event)
        rules_wake.advance(fold, event)
        rules_remote.advance(fold, event)
    if not fold.pending and not fold.open_requests:
        fold.boundaries.add(event.seq)
    return None


def _envelope_error(fold: Fold, event: Event | UnknownEvent) -> ParseError | None:
    # Unknown non-critical events skip every reduce rule, but not the envelope ones.
    if event.branch_id != fold.segment:
        return reject(event, f"branch_id {event.branch_id} differs from its segment's header")
    if event.thread_id != fold.thread_id:
        return reject(event, f"thread_id {event.thread_id} is not the resolved chain's")
    if event.epoch < fold.epoch:
        return reject(event, f"epoch {event.epoch} is lower than the previous {fold.epoch}")
    if event.event_id in fold.event_ids:
        return reject(event, f"event_id {event.event_id} is already on the resolved chain")
    return None
