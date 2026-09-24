"""A new segment's first events, built and checked like any append: the fork event that opens a
child branch, the thread_started that opens a root branch its creator writes itself (a
schedule's thread), and `branch.open` (a team log, a team member)."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from threads.log import BranchId, Header, ParseError, ThreadId
from threads.log.digest import sha256_hex
from threads.reduce import Fold, apply, enter_segment
from threads.result import Err, Ok

# A module, not its names: the index hooks open team logs through this module (a cycle).
from threads.store import indexing, lease
from threads.store.admit import admit
from threads.store.appended import Appended
from threads.store.lines import Draft, Position, event_line, header_line
from threads.store.sql import Branch, branch, insert_branch, insert_events, transaction
from threads.store.verify import StoredEvent

type Built = tuple[StoredEvent, bytes, bytes]
"""An event, its stored line, and the canonical bytes of its content (checked for registered
secrets as it is published, C5)."""


def opening(fold: Fold, header: bytes, draft: Draft, at: Position) -> Ok[Built] | Err[ParseError]:
    """`draft` as the first event of the segment `header` starts, folded into `fold`: the same
    line and rules a writer's append would produce."""
    built = event_line(draft, at)
    if isinstance(built, Err):
        return built
    enter_segment(fold, Header.model_validate_json(header))
    error = apply(fold, built.value[0])
    return Err(error) if error is not None else built


@dataclass(frozen=True, slots=True)
class NewRoot:
    """A root branch and its first event, ready to insert in one transaction."""

    row: Branch
    first: tuple[StoredEvent, bytes]
    content: bytes
    """What the store's thread checks for registered secrets as it publishes the root."""
    opened: frozenset[str]
    """Its event's id, if it opened a turn."""


def new_root(
    tenant_id: str, thread_id: ThreadId, branch_id: BranchId, first: Draft, now: int
) -> Ok[NewRoot] | Err[ParseError]:
    """A new thread's root branch whose first line is `first`, at epoch 1 (the next writer's
    lease takes epoch 2)."""
    header = header_line(thread_id, branch_id, now)
    fold = Fold(now=now)
    enter_segment(fold, Header.model_validate_json(header))
    admitted = admit(fold, (first,), Position(thread_id, branch_id, 1, 1, header, now))
    if isinstance(admitted, Err):
        return admitted
    ((event, line),) = admitted.value.rows
    row = Branch(branch_id, thread_id, tenant_id, None, None, header, "ready", 1, sha256_hex(line))
    return Ok(NewRoot(row, (event, line), admitted.value.content, admitted.value.opened))


def insert_root(conn: sqlite3.Connection, root: NewRoot) -> ParseError | None:
    """Inserts the branch and its first event, and runs the index hooks, in the caller's
    transaction (which rolls back on an error)."""
    insert_branch(conn, root.row)
    insert_events(conn, (root.first,), root.row.head_hash)
    event, row = root.first[0], root.row
    # No lease holds a new root: its first writer takes epoch 2.
    appended = Appended(
        row.tenant_id,
        row.thread_id,
        row.branch_id,
        indexing.known([event]),
        root.opened,
        "",
        event.time,
    )
    return indexing.index_append(conn, appended)


ALREADY_OPEN: Final[Literal["already_open"]] = "already_open"
"""The branch already exists: whoever opened it did this open's work, so the caller is done."""


@dataclass(frozen=True, slots=True)
class BranchOpening:
    """A new root branch, the lease that holds it first (at epoch 1), and its first events."""

    tenant_id: str
    thread_id: ThreadId
    branch_id: BranchId
    lease: lease.Lease
    drafts: Sequence[Draft]


@dataclass(frozen=True, slots=True)
class OpenedBranch:
    """The new branch folded through its first events: what its writer starts from."""

    fold: Fold
    last_line: bytes


def open_branch(
    conn: sqlite3.Connection, o: BranchOpening, now: int
) -> OpenedBranch | Literal["already_open"] | ParseError:
    """`branch.open` in the caller's transaction, which rolls back on an error: inserts the
    thread and branch rows (a thread already stored is refused), admits the drafts as a writer
    would, stores them, takes the first lease and runs the index hooks. A new branch is one
    nobody can hold yet, so this never appends to a live branch."""
    if branch(conn, o.branch_id) is not None:
        return ALREADY_OPEN
    if conn.execute("SELECT 1 FROM threads WHERE thread_id = ?", (o.thread_id,)).fetchone():
        # A new branch opens a new thread: never a second root of one already stored.
        return ParseError("invalid_transition", f"thread {o.thread_id} already exists")
    header = header_line(o.thread_id, o.branch_id, now)
    fold = Fold(now=now)
    enter_segment(fold, Header.model_validate_json(header))
    admitted = admit(fold, o.drafts, Position(o.thread_id, o.branch_id, 1, 1, header, now))
    if isinstance(admitted, Err):
        return admitted.error
    rows = admitted.value.rows
    row = Branch(
        o.branch_id, o.thread_id, o.tenant_id, None, None, header, "ready", 0, sha256_hex(header)
    )
    lease.insert_new(conn, row, o.lease)
    last = rows[-1][1] if rows else header
    insert_events(conn, rows, sha256_hex(last))
    events = indexing.known([event for event, _ in rows])
    opened = admitted.value.opened
    appended = Appended(
        o.tenant_id, o.thread_id, o.branch_id, events, opened, o.lease.holder_id, now
    )
    error = indexing.index_append(conn, appended)
    return error if error is not None else OpenedBranch(fold, last)


class _UndoError(Exception):
    def __init__(self, error: ParseError) -> None:
        self.error = error


def open_alone(
    conn: sqlite3.Connection, o: BranchOpening, now: int
) -> OpenedBranch | Literal["already_open"] | ParseError:
    """`open_branch` in a transaction of its own."""
    try:
        with transaction(conn):
            opened = open_branch(conn, o, now)
            if isinstance(opened, ParseError):
                raise _UndoError(opened)
            return opened
    except _UndoError as undo:
        return undo.error
