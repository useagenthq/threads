"""The pending_wakes index (spec/schema/store.sql; Gate 1 §2.7.3): one row per running background
child of a branch, inserted in the append of its agent_spawned and deleted in the append of its
agent_finished, or of the parent's parked{kind: child} for it (a parked child is resumed by the
control path). A host resumes a branch with rows, so a child a crash stopped still reports and
wakes its parent. Every append writes them through the index hooks (`wake_rows`);
`threads.reduce.wakes.pending_wakes` is the fold that rebuilds them."""

from collections.abc import Callable, Sequence

from threads.log import BranchId, Event, ParseError, ThreadId
from threads.reduce.wakes import change, pending_wakes
from threads.store.appended import Appended
from threads.store.conn import Conn
from threads.store.sql import text_of

_INSERT = (
    "INSERT INTO pending_wakes (branch_id, child_thread_id) VALUES (?, ?) ON CONFLICT DO NOTHING"
)
_DELETE = "DELETE FROM pending_wakes WHERE branch_id = ? AND child_thread_id = ?"


def wake_rows(conn: Conn, a: Appended) -> ParseError | None:
    """The wake rows one append's events insert and delete: an index hook of every append."""
    for event in a.events:
        found = change(event)
        if found is not None:
            added, child = found
            conn.execute(_INSERT if added else _DELETE, (a.branch_id, child))
    return None


def branches(conn: Conn) -> tuple[tuple[str, ThreadId, BranchId], ...]:
    """(tenant, thread, branch) of every branch with a background child still to report: what
    a host resumes."""
    rows = conn.execute(
        "SELECT DISTINCT b.tenant_id, b.thread_id, w.branch_id FROM pending_wakes w"
        " JOIN branches b ON b.branch_id = w.branch_id"
    ).fetchall()
    return tuple(
        (text_of(tenant), ThreadId(text_of(thread)), BranchId(text_of(branch)))
        for tenant, thread, branch in rows
    )


def rebuild(conn: Conn, read: Callable[[BranchId], Sequence[Event]]) -> None:
    """An index wipe's repair: every row again from the logs alone. `read` is each branch's
    resolved events."""
    conn.execute("DELETE FROM pending_wakes")
    found = conn.execute("SELECT branch_id FROM branches").fetchall()
    for (column,) in found:
        branch = BranchId(text_of(column))
        refold(conn, branch, read(branch))


def refold(conn: Conn, branch: BranchId, events: Sequence[Event]) -> None:
    """One branch's wake rows again from its resolved events: a rebuild's or an import's."""
    conn.execute("DELETE FROM pending_wakes WHERE branch_id = ?", (branch,))
    for child in pending_wakes(events, branch):
        conn.execute(_INSERT, (branch, child))
