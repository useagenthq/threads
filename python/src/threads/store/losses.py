"""Loss accounting for telemetry exporters (store.sql `observers` and `observer_losses`): what a
deletion may have dropped before an exporter sent it. Bookkeeping only; the log never reads it."""

from collections.abc import Sequence
from dataclasses import dataclass

from threads.log import ThreadId
from threads.store.conn import Conn
from threads.store.sql import int_of, text_of


@dataclass(frozen=True, slots=True)
class LossRow:
    tenant_id: str
    thread_id: ThreadId
    unchecked_events: int
    deleted_at: int


def record_losses(conn: Conn, tenant_id: str, thread_id: ThreadId, now: int) -> None:
    """In the delete transaction, before the thread's events go: one row per registered
    observer, counting the thread's events past that observer's cursor on each branch. With no
    observer registered it inserts nothing."""
    conn.execute(
        "INSERT INTO observer_losses"
        " (observer, tenant_id, thread_id, unchecked_events, deleted_at, reported_at)"
        " SELECT o.name, ?, ?, ("
        "   SELECT count(*) FROM events e"
        "     JOIN branches b ON b.branch_id = e.branch_id"
        "     LEFT JOIN observer_cursors c ON c.observer = o.name AND c.branch_id = e.branch_id"
        "     WHERE b.thread_id = ? AND e.seq > coalesce(c.seq, 0)"
        " ), ?, NULL"
        # WHERE true: without it SQLite reads ON CONFLICT as the join's ON (its upsert rule).
        " FROM observers o WHERE true"
        " ON CONFLICT DO NOTHING",
        (tenant_id, thread_id, thread_id, now),
    )


def register_observer(conn: Conn, observer: str, now: int) -> None:
    """Registers `observer` once, so deletions from now on record what it may not have sent."""
    conn.execute(
        "INSERT INTO observers (name, registered_at) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (observer, now),
    )


def unreported_losses(conn: Conn, observer: str) -> tuple[LossRow, ...]:
    """The observer's loss rows not yet exported, oldest first."""
    rows: list[tuple[object, ...]] = conn.execute(
        "SELECT tenant_id, thread_id, unchecked_events, deleted_at FROM observer_losses"
        " WHERE observer = ? AND reported_at IS NULL ORDER BY deleted_at, thread_id",
        (observer,),
    ).fetchall()
    return tuple(
        LossRow(text_of(t), ThreadId(text_of(th)), int_of(n), int_of(at)) for t, th, n, at in rows
    )


def mark_reported(conn: Conn, observer: str, rows: Sequence[LossRow], now: int) -> None:
    """After the collector accepted their spans."""
    for row in rows:
        conn.execute(
            "UPDATE observer_losses SET reported_at = ?"
            " WHERE observer = ? AND thread_id = ? AND deleted_at = ? AND reported_at IS NULL",
            (now, observer, row.thread_id, row.deleted_at),
        )
