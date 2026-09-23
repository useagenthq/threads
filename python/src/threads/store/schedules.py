"""The scheduler's rows (store.sql `schedule_threads`, `schedule_occurrences`): a schedule's thread
and its occurrences, every read and write scoped by tenant.

A due occurrence is reserved as a `pending` row with what firing it needs frozen (agent, input,
timezone), then decided under its thread's writer by a conditional update in the transaction of
the append that logs it, so a scheduler holding a stale copy of the row appends nothing."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import TypeAdapter, ValidationError

from threads._generated.host_api_v1 import Input
from threads.log import BranchId, Header, ParseError, ThreadId, ThreadStartedEvent
from threads.log.digest import sha256_hex
from threads.log.jcs import canonicalize
from threads.reduce import Fold, apply, enter_segment
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.store.companion import Companion
from threads.store.lines import Draft, Position, event_line, header_line, uuid7
from threads.store.sql import (
    Branch,
    blob_of,
    insert_branch,
    insert_events,
    int_of,
    text_of,
    transaction,
)
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
        return await self._worker.call(lambda c: _pending(c, tenant, thread_id))

    async def reserve_due(self, started: Draft, due: Sequence[Due], now: int) -> None:
        """Reserves due occurrences on the schedule's thread, in one transaction with finding
        that thread: a deletion commits wholly before (a new thread is made) or after (these rows
        are retired). A schedule without a thread, or whose thread was started with another
        config than `started` pins, gets a new thread (a config change starts a new thread): its
        identity row, branch and thread_started are written together. Another scheduler's
        reservation of a key wins silently."""
        if not due:
            return
        tenant = self._tenant
        thread_id, branch_id = ThreadId(uuid7(now)), BranchId(uuid7(now))
        header = header_line(thread_id, branch_id, now)
        line = _first_line(started, Position(thread_id, branch_id, 1, 1, header, now))
        row = Branch(
            branch_id, thread_id, tenant, None, None, header, "ready", 1, sha256_hex(line[1])
        )
        await self._worker.call(lambda c: _reserve_due(c, row, line, due, now))

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
    return tuple(_parsed(*row) for row in rows)


def _parsed(*row: object) -> Pending:
    """A stored pending row, every column checked: a row that fails is corruption, reported."""
    schedule, at, thread, reason, agent, input_json, timezone = row
    try:
        found = Pending(
            text_of(schedule),
            int_of(at),
            ThreadId(text_of(thread)),
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


def _reason(value: object) -> Reason | None:
    if value is None:
        return None
    for reason in _REASONS:
        if reason == value:
            return reason
    raise TypeError(f"a stored skip reason {value!r} is not one of {_REASONS}")


def _reserve_due(
    conn: sqlite3.Connection,
    new: Branch,
    first: tuple[ThreadStartedEvent, bytes],
    due: Sequence[Due],
    now: int,
) -> None:
    tenant_id, schedule_id = new.tenant_id, due[0].schedule_id
    with transaction(conn):
        found = _thread_of(conn, tenant_id, schedule_id)
        thread = found
        if found is None or _pin_of(conn, tenant_id, found) != first[0].data.config_hash:
            conn.execute(
                "INSERT INTO schedule_threads (tenant_id, schedule_id, thread_id, created_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT (tenant_id, schedule_id)"
                " DO UPDATE SET thread_id = excluded.thread_id, created_at = excluded.created_at",
                (tenant_id, schedule_id, new.thread_id, now),
            )
            insert_branch(conn, new)
            insert_events(conn, (first,), new.head_hash)
            thread = new.thread_id
        conn.executemany(
            "INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at, state,"
            " reason, thread_id, claimed_at, agent, input_json, timezone)"
            " VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            [
                (
                    tenant_id,
                    d.schedule_id,
                    d.occurrence_at,
                    "missed" if d.missed else None,
                    thread,
                    now,
                    d.agent,
                    _canonical(d.input),
                    d.timezone,
                )
                for d in due
            ],
        )


def _thread_of(conn: sqlite3.Connection, tenant_id: str, schedule_id: str) -> ThreadId | None:
    found: tuple[object] | None = conn.execute(
        "SELECT thread_id FROM schedule_threads WHERE tenant_id = ? AND schedule_id = ?",
        (tenant_id, schedule_id),
    ).fetchone()
    return None if found is None else ThreadId(text_of(found[0]))


def _pin_of(conn: sqlite3.Connection, tenant_id: str, thread_id: ThreadId) -> str | None:
    """The config_hash the thread was started with, parsed from its stored line."""
    found: tuple[object] | None = conn.execute(
        "SELECT e.line FROM branches b JOIN events e ON e.branch_id = b.branch_id"
        " WHERE b.thread_id = ? AND b.tenant_id = ? AND b.parent_branch_id IS NULL"
        " AND e.type = 'thread_started'",
        (thread_id, tenant_id),
    ).fetchone()
    if found is None:
        return None
    return ThreadStartedEvent.model_validate_json(blob_of(found[0])).data.config_hash


def _first_line(first: Draft, at: Position) -> tuple[ThreadStartedEvent, bytes]:
    """A new branch's thread_started, checked like any append: failing is a bug, not an
    outcome."""
    built = event_line(first, at)
    if isinstance(built, Err):
        raise ValueError(built.error.message)
    event = built.value[0]
    if not isinstance(event, ThreadStartedEvent):
        raise TypeError("a schedule thread opens with thread_started")
    fold = Fold(now=at.now)
    enter_segment(fold, Header.model_validate_json(at.prev_line))
    error = apply(fold, event)
    if error is not None:
        raise ValueError(error.message)
    return event, built.value[1]


def _canonical(given: Input) -> str:
    text = canonicalize(given if isinstance(given, str) else [to_json(p) for p in given])
    if not isinstance(text, Ok):
        raise AssertionError("a parsed input is canonical JSON")
    return text.value
