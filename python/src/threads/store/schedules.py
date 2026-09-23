"""The scheduler's rows (store.sql `schedule_threads`, `schedule_occurrences`): a schedule's one
thread and its occurrences, every read and write scoped by tenant.

A due occurrence is reserved as a `pending` row with what firing it needs frozen (agent, input,
timezone), then decided under its thread's writer by a conditional update in the transaction of
the append that logs it, so a scheduler holding a stale copy of the row appends nothing."""

import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from threads.log import BranchId, Header, ParseError, ThreadId
from threads.log.digest import sha256_hex
from threads.reduce import Fold, apply, enter_segment
from threads.result import Err
from threads.store.companion import Companion
from threads.store.lines import Draft, Position, event_line, header_line, uuid7
from threads.store.sql import Branch, insert_branch, insert_events, int_of, text_of, transaction
from threads.store.verify import StoredEvent
from threads.store.worker import Worker

type Reason = Literal["missed", "overlap", "removed"]
_REASONS: Final[tuple[Reason, ...]] = ("missed", "overlap", "removed")
_PENDING: Final = (
    "SELECT schedule_id, occurrence_at, thread_id, reason, agent, input_json, timezone"
    " FROM schedule_occurrences WHERE tenant_id = ? AND state = 'pending'"
)
_ORDER: Final = " ORDER BY occurrence_at, thread_id, schedule_id"


@dataclass(frozen=True, slots=True)
class Pending:
    """A reserved occurrence not yet decided, with what firing it needs frozen at reservation."""

    schedule_id: str
    occurrence_at: int
    thread_id: ThreadId
    reason: Reason | None
    """`missed` from reservation, `overlap` once the thread's log showed a turn open."""
    agent: str
    input_json: str
    """The schedule's input as canonical JSON."""
    timezone: str


class ScheduleRows:
    """One tenant's schedule rows, each call one statement or transaction on the store's thread."""

    def __init__(self, worker: Worker, tenant_id: str) -> None:
        self._worker = worker
        self._tenant = tenant_id

    async def pending(self, thread_id: ThreadId | None = None) -> tuple[Pending, ...]:
        """Pending rows in occurrence order: the tenant's, whatever schedules are configured
        now, or one thread's."""
        tenant = self._tenant
        return await self._worker.call(lambda c: _pending(c, tenant, thread_id))

    async def reserve(self, row: Pending, now: int) -> None:
        """Reserves a due occurrence; another scheduler's reservation of the key wins silently."""
        tenant = self._tenant
        await self._worker.call(lambda c: _reserve(c, tenant, row, now))

    async def mark_overlaps(self, thread_id: ThreadId) -> None:
        """The thread's log shows a turn still open: its undecided pending rows are overlaps."""
        tenant = self._tenant
        await self._worker.call(
            lambda c: c.execute(
                "UPDATE schedule_occurrences SET reason = 'overlap' WHERE tenant_id = ?"
                " AND thread_id = ? AND state = 'pending' AND reason IS NULL",
                (tenant, thread_id),
            )
        )

    async def last(self, schedule_id: str) -> int | None:
        """The schedule's latest reserved occurrence, in any state."""
        tenant = self._tenant

        def latest(conn: sqlite3.Connection) -> int | None:
            (found,) = conn.execute(
                "SELECT max(occurrence_at) FROM schedule_occurrences"
                " WHERE tenant_id = ? AND schedule_id = ?",
                (tenant, schedule_id),
            ).fetchone()
            return None if found is None else int_of(found)

        return await self._worker.call(latest)

    async def threads(self) -> tuple[ThreadId, ...]:
        """The threads of the tenant's schedules."""
        tenant = self._tenant
        rows: list[tuple[object]] = await self._worker.call(
            lambda c: c.execute(
                "SELECT thread_id FROM schedule_threads WHERE tenant_id = ?", (tenant,)
            ).fetchall()
        )
        return tuple(ThreadId(text_of(t)) for (t,) in rows)

    async def thread(
        self, schedule_id: str, first: Callable[[], Awaitable[Draft]], now: int
    ) -> ThreadId:
        """The schedule's thread, created on first use in one transaction with its identity row,
        branch and `first` event (its thread_started). A scheduler that loses the identity insert
        stores nothing and uses the winner's thread: never an orphan branch or a second thread."""
        tenant = self._tenant
        found = await self._worker.call(lambda c: _thread_of(c, tenant, schedule_id))
        if found is not None:
            return found
        thread_id, branch_id = ThreadId(uuid7(now)), BranchId(uuid7(now))
        header = header_line(thread_id, branch_id, now)
        line = _first_line(await first(), header, Position(thread_id, branch_id, 1, 1, header, now))
        row = Branch(
            branch_id, thread_id, tenant, None, None, header, "ready", 1, sha256_hex(line[1])
        )
        return await self._worker.call(
            lambda c: _create_thread(c, (tenant, schedule_id, now), row, line)
        )


def decided(tenant_id: str, row: Pending, reason: Reason | None) -> Companion:
    """Decides the row in the transaction of the append that logs it; a row that is no longer
    pending (another scheduler decided it) refuses, and the append rolls back."""

    def run(conn: sqlite3.Connection, events: Sequence[StoredEvent]) -> ParseError | None:
        done = conn.execute(
            "UPDATE schedule_occurrences SET state = ?, reason = ?, logged_seq = ?"
            " WHERE tenant_id = ? AND schedule_id = ? AND occurrence_at = ? AND state = 'pending'",
            (
                "fired" if reason is None else "skipped",
                reason,
                events[0].seq,
                tenant_id,
                row.schedule_id,
                row.occurrence_at,
            ),
        )
        return None if done.rowcount == 1 else ParseError("invalid_request", "decided elsewhere")

    return run


def _pending(
    conn: sqlite3.Connection, tenant_id: str, thread_id: ThreadId | None
) -> tuple[Pending, ...]:
    if thread_id is None:
        rows: list[tuple[object, ...]] = conn.execute(_PENDING + _ORDER, (tenant_id,)).fetchall()
    else:
        where = _PENDING + " AND thread_id = ?" + _ORDER
        rows = conn.execute(where, (tenant_id, thread_id)).fetchall()
    return tuple(
        Pending(
            text_of(schedule),
            int_of(at),
            ThreadId(text_of(thread)),
            _reason(reason),
            text_of(agent),
            text_of(input_json),
            text_of(timezone),
        )
        for schedule, at, thread, reason, agent, input_json, timezone in rows
    )


def _reason(value: object) -> Reason | None:
    if value is None:
        return None
    for reason in _REASONS:
        if reason == value:
            return reason
    raise TypeError(f"a stored skip reason {value!r} is not one of {_REASONS}")


def _reserve(conn: sqlite3.Connection, tenant_id: str, row: Pending, now: int) -> None:
    conn.execute(
        "INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at, state, reason,"
        " thread_id, claimed_at, agent, input_json, timezone)"
        " VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (
            tenant_id,
            row.schedule_id,
            row.occurrence_at,
            row.reason,
            row.thread_id,
            now,
            row.agent,
            row.input_json,
            row.timezone,
        ),
    )


def _thread_of(conn: sqlite3.Connection, tenant_id: str, schedule_id: str) -> ThreadId | None:
    found: tuple[object] | None = conn.execute(
        "SELECT thread_id FROM schedule_threads WHERE tenant_id = ? AND schedule_id = ?",
        (tenant_id, schedule_id),
    ).fetchone()
    return None if found is None else ThreadId(text_of(found[0]))


def _first_line(first: Draft, header: bytes, at: Position) -> tuple[StoredEvent, bytes]:
    """A new branch's first event, checked like any append: failing is a bug, not an outcome."""
    built = event_line(first, at)
    if isinstance(built, Err):
        raise ValueError(built.error.message)
    fold = Fold(now=at.now)
    enter_segment(fold, Header.model_validate_json(header))
    error = apply(fold, built.value[0])
    if error is not None:
        raise ValueError(error.message)
    return built.value


def _create_thread(
    conn: sqlite3.Connection,
    key: tuple[str, str, int],
    row: Branch,
    first: tuple[StoredEvent, bytes],
) -> ThreadId:
    tenant_id, schedule_id, now = key
    with transaction(conn):
        won = conn.execute(
            "INSERT INTO schedule_threads (tenant_id, schedule_id, thread_id, created_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (tenant_id, schedule_id, row.thread_id, now),
        )
        if won.rowcount == 1:
            insert_branch(conn, row)
            insert_events(conn, (first,), row.head_hash)
            return row.thread_id
    winner = _thread_of(conn, tenant_id, schedule_id)
    if winner is None:
        raise AssertionError("a lost identity insert has a winner")
    return winner
