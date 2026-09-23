"""Retention: explicit deletion and the artifact sweep, never automatic.

Deleting a thread removes its log rows and projections together, every branch at once, so no
child loses the chain it forks from, moves the thread's live provider resources to `releasing`
for the next gc, and writes a tombstone, all in one transaction. The resource ledger is never
deleted with log rows: it owns cleanup. The artifact sweep keeps every artifact any stored line
or branch row names.
"""

import re
import sqlite3
import time
from pathlib import Path
from typing import Final

from threads.log import Parent, ThreadId, ThreadStartedEvent
from threads.store.sql import blob_of, text_of, transaction

GRACE_S: Final = 7 * 24 * 3600
"""An unreferenced artifact younger than this may belong to an append in flight: kept."""
_SHA256: Final = re.compile(rb"[0-9a-f]{64}")
_PER_BRANCH: Final = ("events", "leases", "observer_cursors")


def threads_of(conn: sqlite3.Connection, tenant_id: str) -> tuple[ThreadId, ...]:
    rows: list[tuple[object]] = conn.execute(
        "SELECT thread_id FROM threads WHERE tenant_id = ?", (tenant_id,)
    ).fetchall()
    return tuple(ThreadId(text_of(t)) for (t,) in rows)


def delete_thread(conn: sqlite3.Connection, tenant_id: str, thread_id: ThreadId, now: int) -> int:
    """Deletes one thread of the tenant and, recursively, its subagent threads (never a handoff
    target), each with its tombstone, in one transaction; the number of branches removed (0: no
    such thread, nothing written)."""
    with transaction(conn):
        doomed = _with_subagents(conn, tenant_id, thread_id)
        return sum(_delete_one(conn, tenant_id, t, now) for t in doomed)


def _with_subagents(conn: sqlite3.Connection, tenant_id: str, root: ThreadId) -> list[ThreadId]:
    """The thread and every thread whose thread_started names it (or a found one) as a subagent
    parent, same tenant only (spec, Deleting a thread)."""
    rows: list[tuple[object, object]] = conn.execute(
        "SELECT b.thread_id, e.line FROM events e JOIN branches b ON b.branch_id = e.branch_id"
        " WHERE b.tenant_id = ? AND e.type = 'thread_started'",
        (tenant_id,),
    ).fetchall()
    parent_of: dict[str, str] = {}
    for thread, line in rows:
        parent = ThreadStartedEvent.model_validate_json(blob_of(line)).data.parent
        if isinstance(parent, Parent) and parent.relation == "subagent":
            parent_of[text_of(thread)] = parent.thread_id
    found = [root]
    for current in found:  # grows while iterating: a breadth-first walk down the tree
        found += [ThreadId(c) for c, p in parent_of.items() if p == current and c not in found]
    return found


def _delete_one(conn: sqlite3.Connection, tenant_id: str, thread_id: ThreadId, now: int) -> int:
    rows: list[tuple[object]] = conn.execute(
        "SELECT branch_id FROM branches WHERE thread_id = ? AND tenant_id = ?",
        (thread_id, tenant_id),
    ).fetchall()
    branches = [text_of(b) for (b,) in rows]
    for branch in branches:
        for table in _PER_BRANCH:
            conn.execute(f"DELETE FROM {table} WHERE branch_id = ?", (branch,))  # noqa: S608
        conn.execute("DELETE FROM budget_ledger WHERE attempt_key LIKE ?", (f"{branch}:%",))
        conn.execute(
            "UPDATE resources SET state = 'releasing' WHERE owner_branch_id = ? AND state = 'live'",
            (branch,),
        )
    for table in ("approvals", "inbox", "channel_threads", "run_receipts"):
        conn.execute(
            f"DELETE FROM {table} WHERE thread_id = ? AND tenant_id = ?",  # noqa: S608
            (thread_id, tenant_id),
        )
    conn.execute(
        "DELETE FROM branches WHERE thread_id = ? AND tenant_id = ?", (thread_id, tenant_id)
    )
    gone = conn.execute(
        "DELETE FROM threads WHERE thread_id = ? AND tenant_id = ?", (thread_id, tenant_id)
    )
    if gone.rowcount:
        conn.execute(
            "INSERT INTO tombstones (thread_id, tenant_id, deleted_at) VALUES (?, ?, ?)"
            " ON CONFLICT DO NOTHING",
            (thread_id, tenant_id, now),
        )
    return len(branches)


def referenced(conn: sqlite3.Connection) -> frozenset[str]:
    """Every sha256 any stored line or branch row names: the mark of the sweep. A hash-shaped
    string that is not an artifact only keeps a file that doesn't exist."""
    found: set[str] = set()
    for (line,) in conn.execute("SELECT line FROM events"):
        found.update(m.decode() for m in _SHA256.findall(blob_of(line)))
    for header, dropped in conn.execute("SELECT header_line, dropped_ref FROM branches"):
        found.update(m.decode() for m in _SHA256.findall(blob_of(header)))
        if dropped is not None:
            found.add(text_of(dropped))
    return frozenset(found)


def sweep(root: Path, keep: frozenset[str], grace_s: float = GRACE_S) -> tuple[str, ...]:
    """Removes unreferenced artifacts older than the grace period; returns their hashes."""
    removed: list[str] = []
    cutoff = time.time() - grace_s
    for path in sorted(root.glob("sha256/*/*")):
        if path.name in keep or path.stat().st_mtime > cutoff:
            continue
        path.unlink()
        removed.append(path.name)
    return tuple(removed)
