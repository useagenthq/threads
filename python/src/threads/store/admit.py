"""Admitting a batch of drafts: each built as its stored line and folded through `validate_next`,
the same checks as an import. A writer's append and `branch.open` both store what this admits."""

import copy
from collections.abc import Sequence
from dataclasses import dataclass

from threads.log import ParseError
from threads.reduce import Fold, apply
from threads.result import Err, Ok
from threads.store.lines import Draft, Position, event_line
from threads.store.verify import StoredEvent


@dataclass(frozen=True, slots=True)
class Admitted:
    rows: tuple[tuple[StoredEvent, bytes], ...]
    content: bytes
    """The canonical bytes of the events' content, one per line: checked again for registered
    secrets as the store publishes them (C5)."""
    opened: frozenset[str]
    """The ids of the events that opened a turn."""


def admit(fold: Fold, drafts: Sequence[Draft], at: Position) -> Ok[Admitted] | Err[ParseError]:
    """Folds `drafts` into `fold` in order, the first at `at`. On an error `fold` may hold the
    drafts before it: the caller folds the committed log again, or drops a trial copy."""
    rows: list[tuple[StoredEvent, bytes]] = []
    content: list[bytes] = []
    opened: set[str] = set()
    prev = at.prev_line
    for draft in drafts:
        here = Position(at.thread_id, at.branch_id, fold.seq + 1, at.epoch, prev, at.now)
        built = event_line(draft, here)
        if isinstance(built, Err):
            return built
        before = fold.in_turn
        error = apply(fold, built.value[0])
        if error is not None:
            return Err(error)
        event, line, stored = built.value
        if fold.in_turn and not before:
            opened.add(event.event_id)
        rows.append((event, line))
        content.append(stored)
        prev = line
    return Ok(Admitted(tuple(rows), b"\n".join(content), frozenset(opened)))


def trial(fold: Fold) -> Fold:
    """A copy of `fold` to admit into, sharing its immutable events."""
    # ponytail: copies the fold's containers on every decided append; keep an undo log instead if
    # decided batches get hot.
    return copy.deepcopy(fold, {id(e): e for e in fold.events})
