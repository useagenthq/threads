"""The scheduler's rows (store.sql `schedule_threads`, `schedule_occurrences`): the threads a
schedule has had and its occurrences, every read and write scoped by tenant. SQL only: which
thread a schedule uses is decided in `threads.host.schedule_threads`.

A due occurrence is reserved as a `pending` row with what firing it needs frozen (agent, input,
timezone), then decided under its thread's writer by a conditional update in the transaction of
the append that logs it, so a scheduler holding a stale copy of the row appends nothing."""

import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import TypeAdapter, ValidationError

from threads._generated.host_api_v1 import Input
from threads.log import ParseError, ThreadId
from threads.log.jcs import canonicalize
from threads.reduce.handlers import to_json
from threads.result import Ok
from threads.store.companion import Companion
from threads.store.sql import int_of, text_of
from threads.store.verify import StoredEvent
from threads.store.worker import Worker

type Reason = Literal["missed", "overlap", "removed"]
_REASONS: Final[tuple[Reason, ...]] = ("missed", "overlap", "removed")
_INPUT: TypeAdapter[Input] = TypeAdapter(Input, config={"strict": True})
_PENDING: Final = (
    "SELECT schedule_id, occurrence_at, thread_id, reason, agent, input_json, timezone"
    " FROM schedule_occurrences WHERE tenant_id = ? AND state = 'pending'"
)
_ORDER: Final = " ORDER BY occurrence_at, thread_id, schedule_id"


@dataclass(frozen=True, slots=True)
class Due:
    """A due occurrence to reserve on its schedule's thread."""

    schedule_id: str
    occurrence_at: int
    agent: str
    input: Input
    timezone: str
    missed: bool


@dataclass(frozen=True, slots=True)
class Pending:
    """A reserved occurrence not yet decided, with what firing it needs frozen at reservation."""

    schedule_id: str
    occurrence_at: int
    thread_id: ThreadId
    reason: Reason | None
    """`missed` from reservation, `overlap` once the thread's log showed a turn open."""
    agent: str
    input: Input
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
        return await self._worker.call(lambda c: pending_rows(c, tenant, thread_id))

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
        """Every thread the tenant's schedules have had: what recovery walks."""
        tenant = self._tenant
        rows: list[tuple[object]] = await self._worker.call(
            lambda c: c.execute(
                "SELECT DISTINCT thread_id FROM schedule_threads WHERE tenant_id = ?", (tenant,)
            ).fetchall()
        )
        return tuple(_thread_id(t) for (t,) in rows)


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


def pending_rows(
    conn: sqlite3.Connection, tenant_id: str, thread_id: ThreadId | None = None
) -> tuple[Pending, ...]:
    """Pending rows in occurrence order: the tenant's, or one thread's."""
    if thread_id is None:
        rows: list[tuple[object, ...]] = conn.execute(_PENDING + _ORDER, (tenant_id,)).fetchall()
    else:
        where = _PENDING + " AND thread_id = ?" + _ORDER
        rows = conn.execute(where, (tenant_id, thread_id)).fetchall()
    return tuple(_parsed(*row) for row in rows)


def _parsed(*row: object) -> Pending:
    """A stored pending row, every column checked: a row that fails is corruption, reported."""
    schedule, at, thread, reason, agent, input_json, timezone = row
    try:
        found = Pending(
            text_of(schedule),
            int_of(at),
            _thread_id(thread),
            _reason(reason),
            text_of(agent),
            _INPUT.validate_json(text_of(input_json)),
            text_of(timezone),
        )
    except (TypeError, ValidationError) as error:
        raise TypeError(f"schedule rows are corrupt: {error}") from None
    if not (found.schedule_id and found.thread_id and found.agent and found.timezone):
        raise TypeError(f"schedule rows are corrupt: an empty column in {row!r}")
    return found


def _thread_id(value: object) -> ThreadId:
    """A stored thread id, in the schema's lowercase UUID form."""
    text = text_of(value)
    try:
        canonical = str(uuid.UUID(text))
    except ValueError:
        canonical = ""
    if canonical != text:
        raise TypeError(f"a stored thread id {text!r} is not a lowercase UUID")
    return ThreadId(text)


def _reason(value: object) -> Reason | None:
    if value is None:
        return None
    for reason in _REASONS:
        if reason == value:
            return reason
    raise TypeError(f"a stored skip reason {value!r} is not one of {_REASONS}")


def current_thread(conn: sqlite3.Connection, tenant_id: str, schedule_id: str) -> ThreadId | None:
    """The schedule's current thread."""
    found: tuple[object] | None = conn.execute(
        "SELECT thread_id FROM schedule_threads"
        " WHERE tenant_id = ? AND schedule_id = ? AND current = 1",
        (tenant_id, schedule_id),
    ).fetchone()
    return None if found is None else _thread_id(found[0])


def reserved(conn: sqlite3.Connection, tenant_id: str, due: Due) -> bool:
    """Whether the occurrence's key is already reserved, in any state."""
    found = conn.execute(
        "SELECT 1 FROM schedule_occurrences"
        " WHERE tenant_id = ? AND schedule_id = ? AND occurrence_at = ?",
        (tenant_id, due.schedule_id, due.occurrence_at),
    ).fetchone()
    return found is not None


def insert_pending(
    conn: sqlite3.Connection, tenant_id: str, thread_id: ThreadId, due: Due, now: int
) -> None:
    """Inserts a pending row; a key another scheduler reserved first wins silently."""
    conn.execute(
        "INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at, state,"
        " reason, thread_id, claimed_at, agent, input_json, timezone)"
        " VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (
            tenant_id,
            due.schedule_id,
            due.occurrence_at,
            "missed" if due.missed else None,
            thread_id,
            now,
            due.agent,
            _canonical(due.input),
            due.timezone,
        ),
    )


def make_current(
    conn: sqlite3.Connection, tenant_id: str, schedule_id: str, thread_id: ThreadId, now: int
) -> None:
    """Makes `thread_id` the schedule's current thread; the old one stays listed for recovery."""
    conn.execute(
        "UPDATE schedule_threads SET current = 0"
        " WHERE tenant_id = ? AND schedule_id = ? AND current = 1",
        (tenant_id, schedule_id),
    )
    conn.execute(
        "INSERT INTO schedule_threads (tenant_id, schedule_id, thread_id, current, created_at)"
        " VALUES (?, ?, ?, 1, ?)",
        (tenant_id, schedule_id, thread_id, now),
    )


def _canonical(given: Input) -> str:
    text = canonicalize(given if isinstance(given, str) else [to_json(p) for p in given])
    if not isinstance(text, Ok):
        raise AssertionError("a parsed input is canonical JSON")
    return text.value
