"""The pending_wakes index (spec/schema/store.sql; Gate 1 §2.7.3): one row per running background
child of a branch, inserted in the append of its agent_spawned and deleted in the append of its
agent_finished, or of the parent's parked{kind: child} for it (a parked child is resumed by the
control path). A host resumes a branch with rows, so a child a crash stopped still reports and
wakes its parent. The rows follow the replay rule: `threads.reduce.wakes.pending_wakes` is the
fold that rebuilds them."""

import sqlite3
from collections.abc import Callable, Sequence

from threads.log import BranchId, Event, ThreadId
from threads.reduce.wakes import change, pending_wakes
from threads.store.sql import text_of
from threads.store.verify import StoredEvent

_INSERT = "INSERT OR IGNORE INTO pending_wakes (branch_id, child_thread_id) VALUES (?, ?)"
_DELETE = "DELETE FROM pending_wakes WHERE branch_id = ? AND child_thread_id = ?"


def record(conn: sqlite3.Connection, events: Sequence[StoredEvent]) -> None:
    """Runs inside an append's transaction, after its events are inserted."""
    for event in events:
        found = change(event)
        if found is not None:
            added, child = found
            conn.execute(_INSERT if added else _DELETE, (event.branch_id, child))


def branches(conn: sqlite3.Connection) -> tuple[tuple[str, ThreadId, BranchId], ...]:
    """(tenant, thread, branch) of every branch with a background child still to report: what
    a host resumes."""
    rows: list[tuple[object, object, object]] = conn.execute(
        "SELECT DISTINCT b.tenant_id, b.thread_id, w.branch_id FROM pending_wakes w"
        " JOIN branches b ON b.branch_id = w.branch_id"
    ).fetchall()
    return tuple(
        (text_of(tenant), ThreadId(text_of(thread)), BranchId(text_of(branch)))
        for tenant, thread, branch in rows
    )


def rebuild(conn: sqlite3.Connection, read: Callable[[BranchId], Sequence[Event]]) -> None:
    """An index wipe's repair: every row again from the logs alone. `read` is each branch's
    resolved events."""
    conn.execute("DELETE FROM pending_wakes")
    found: list[tuple[object]] = conn.execute("SELECT branch_id FROM branches").fetchall()
    for (column,) in found:
        branch = BranchId(text_of(column))
        for child in pending_wakes(read(branch), branch):
            conn.execute(_INSERT, (branch, child))
