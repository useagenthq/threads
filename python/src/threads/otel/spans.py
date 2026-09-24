"""`spans(branch, lookup)`: a pure function of the log (spec/otel/README.md). The same chain and
the same other chains always give the same spans, ids and attributes."""

from collections.abc import Callable
from dataclasses import dataclass

from threads.log import Event, Parent, UnknownEvent
from threads.otel.span import Link, Span
from threads.otel.turns import ParentOf
from threads.otel.walk import Walked, WalkInput, walk
from threads.store.verify import VerifiedLog
from threads.team.index import turn_openers

type Lookup = Callable[[str], VerifiedLog | None]
"""Another branch's resolved chain (a parent's), or None when it is missing or unreadable."""


@dataclass(frozen=True, slots=True)
class Branch:
    tenant: str
    branch_id: str
    log: VerifiedLog
    """The branch's resolved chain: a fork's includes its parent's prefix."""
    content: bool
    through: int | None = None
    """Read the chain only through this seq (None: all of it)."""


def _events(log: VerifiedLog, through: int | None) -> list[Event]:
    return [
        e
        for segment in log.segments
        for e, _ in segment.events
        if not isinstance(e, UnknownEvent) and (through is None or e.seq <= through)
    ]


def _walk(branch: Branch, branch_id: str, log: VerifiedLog, parent_of: ParentOf) -> Walked:
    given = WalkInput(
        branch.tenant,
        branch_id,
        _events(log, branch.through if log is branch.log else None),
        turn_openers(log),
        branch.content,
        parent_of,
    )
    return walk(given)


def spans(branch: Branch, lookup: Lookup) -> tuple[Span, ...]:
    """The spans that close in the branch's own segment, in close order."""
    walked = _walk(branch, branch.branch_id, branch.log, _parents(lookup, branch))
    fork_at = max(
        (
            e.seq
            for segment in branch.log.segments
            for e, _ in segment.events
            if e.branch_id != branch.branch_id
        ),
        default=0,
    )
    return tuple(s for s in walked.spans if s.close_seq > fork_at)


def _parents(lookup: Lookup, branch: Branch) -> ParentOf:
    """Resolves a child's parent span through the parent's own walk, each chain walked once."""
    walked: dict[str, Walked | None] = {}

    def of(parent: Parent) -> Link | None:
        if parent.branch_id not in walked:
            log = lookup(parent.branch_id)
            walked[parent.branch_id] = (
                None if log is None else _walk(branch, parent.branch_id, log, of)
            )
        found = walked[parent.branch_id]
        return None if found is None else found.anchors.get(parent.event_id)

    return of
