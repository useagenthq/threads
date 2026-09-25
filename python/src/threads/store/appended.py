"""One append as the index hooks see it (`threads.store.indexing`)."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from threads.log import BranchId, Event, ParseError, ThreadId
from threads.store.conn import Conn


@dataclass(frozen=True, slots=True)
class Appended:
    """An append inside its transaction, after its event rows."""

    tenant_id: str
    thread_id: ThreadId
    branch_id: BranchId
    events: Sequence[Event]
    """The appended known events: an unknown non-critical event writes no row."""
    opened: frozenset[str]
    """The ids of the appended events that opened a turn."""
    holder_id: str
    """The appending lease's holder."""
    now: int


type IndexHook = Callable[[Conn, Appended], ParseError | None]
"""Writes index rows from an append's events (the replay rule). An error rolls the whole append
back, as a companion's refusal does."""
