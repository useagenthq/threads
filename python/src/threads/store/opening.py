"""A new segment's first event, built and checked like any append: the fork event that opens a
child branch, or the thread_started that opens a root branch its creator writes itself (a
schedule's thread)."""

import sqlite3
from dataclasses import dataclass

from threads.log import BranchId, Header, ParseError, ThreadId
from threads.log.digest import sha256_hex
from threads.reduce import Fold, apply, enter_segment
from threads.result import Err, Ok
from threads.store.lines import Draft, Position, event_line, header_line
from threads.store.sql import Branch, insert_branch, insert_events
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


def new_root(
    tenant_id: str, thread_id: ThreadId, branch_id: BranchId, first: Draft, now: int
) -> Ok[NewRoot] | Err[ParseError]:
    """A new thread's root branch whose first line is `first`, at epoch 1 (the next writer's
    lease takes epoch 2)."""
    header = header_line(thread_id, branch_id, now)
    at = Position(thread_id, branch_id, 1, 1, header, now)
    built = opening(Fold(now=now), header, first, at)
    if isinstance(built, Err):
        return built
    event, line, content = built.value
    row = Branch(branch_id, thread_id, tenant_id, None, None, header, "ready", 1, sha256_hex(line))
    return Ok(NewRoot(row, (event, line), content))


def insert_root(conn: sqlite3.Connection, root: NewRoot) -> None:
    """Inserts the branch and its first event, in the caller's transaction."""
    insert_branch(conn, root.row)
    insert_events(conn, (root.first,), root.row.head_hash)
