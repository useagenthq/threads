"""The host rows an import claims besides its events: a deleted thread's tombstone, which
refuses the import, and a schedule thread's identity and occurrences, which it rebuilds from the
chain's own schedule events (spec/schema/README.md, "Portable bundles")."""

import time
from collections.abc import Sequence
from dataclasses import dataclass

from threads.log import ParseError, ScheduleFiredEvent, ScheduleSkippedEvent, ThreadId
from threads.store.conn import Conn
from threads.store.verify import StoredEvent


def tombstoned(conn: Conn, tenant_id: str, thread_ids: Sequence[ThreadId]) -> ThreadId | None:
    """The first of `thread_ids` this tenant has deleted, if any."""
    for thread_id in thread_ids:
        found = conn.execute(
            "SELECT 1 FROM tombstones WHERE thread_id = ? AND tenant_id = ?",
            (thread_id, tenant_id),
        ).fetchone()
        if found is not None:
            return thread_id
    return None


def deleted(thread_id: ThreadId) -> ParseError:
    return ParseError("branch_exists", f"thread {thread_id} was deleted from this store")


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One decided occurrence, as its logged event records it."""

    schedule_id: str
    occurrence_at: int
    thread_id: ThreadId
    state: str
    reason: str | None
    logged_seq: int


def schedule_rows(events: Sequence[StoredEvent]) -> tuple[Occurrence, ...]:
    """The schedule rows a chain implies. Every occurrence it carries is decided, so the frozen
    `agent`, `input_json` and `timezone` a pending row needs stay null."""
    rows: list[Occurrence] = []
    for event in events:
        if isinstance(event, ScheduleFiredEvent):
            rows.append(
                Occurrence(
                    event.data.schedule_id,
                    event.data.scheduled_for,
                    event.thread_id,
                    "fired",
                    None,
                    event.seq,
                )
            )
        elif isinstance(event, ScheduleSkippedEvent):
            rows.append(
                Occurrence(
                    event.data.schedule_id,
                    event.data.scheduled_for,
                    event.thread_id,
                    "skipped",
                    event.data.reason,
                    event.seq,
                )
            )
    return tuple(rows)


def claim_schedules(conn: Conn, tenant_id: str, events: Sequence[StoredEvent]) -> ParseError | None:
    """Claims the chain's schedule identity and occurrence keys inside the import's row
    transaction. Each key is inserted with ON CONFLICT DO NOTHING and read back: one that now
    belongs to another thread, or holds another occurrence, was claimed by a scheduler or a
    second importer after prevalidation, so the whole import rolls back."""
    rows = schedule_rows(events)
    now = int(time.time() * 1000)
    for schedule_id, thread_id in {r.schedule_id: r.thread_id for r in rows}.items():
        error = _claim_identity(conn, tenant_id, schedule_id, thread_id, now)
        if error is not None:
            return error
    for row in rows:
        error = _claim_occurrence(conn, tenant_id, row, now)
        if error is not None:
            return error
    return None


def _claim_identity(
    conn: Conn, tenant_id: str, schedule_id: str, thread_id: ThreadId, now: int
) -> ParseError | None:
    conn.execute(
        "INSERT INTO schedule_threads (tenant_id, schedule_id, thread_id, current, created_at)"
        " VALUES (?, ?, ?, 1, ?) ON CONFLICT DO NOTHING",
        (tenant_id, schedule_id, thread_id, now),
    )
    found = conn.execute(
        "SELECT thread_id FROM schedule_threads"
        " WHERE tenant_id = ? AND schedule_id = ? AND current = 1",
        (tenant_id, schedule_id),
    ).fetchone()
    if found is None or str(found[0]) == str(thread_id):
        return None
    return ParseError("schedule_conflict", f"schedule {schedule_id} runs on another thread here")


def _claim_occurrence(conn: Conn, tenant_id: str, row: Occurrence, now: int) -> ParseError | None:
    conn.execute(
        "INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at, state,"
        " reason, thread_id, claimed_at, logged_seq)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (
            tenant_id,
            row.schedule_id,
            row.occurrence_at,
            row.state,
            row.reason,
            row.thread_id,
            now,
            row.logged_seq,
        ),
    )
    stored = conn.execute(
        "SELECT thread_id, state, reason FROM schedule_occurrences"
        " WHERE tenant_id = ? AND schedule_id = ? AND occurrence_at = ?",
        (tenant_id, row.schedule_id, row.occurrence_at),
    ).fetchone()
    held = None if stored is None else (str(stored[0]), str(stored[1]), _text(stored[2]))
    if held == (str(row.thread_id), row.state, row.reason):
        return None
    where = f"{row.schedule_id}@{row.occurrence_at}"
    return ParseError("schedule_conflict", f"occurrence {where} is decided otherwise here")


def _text(value: object) -> str | None:
    return None if value is None else str(value)
