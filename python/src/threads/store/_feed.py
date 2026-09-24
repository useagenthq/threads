"""The store reader a telemetry exporter builds on (`threads.otel`). Private: not in
spec/api.json, not documented, and it may change in any release. It never takes a lease and never
appends: its only writes are observer bookkeeping."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from threads.log import BranchId, ParseError, ThreadId
from threads.result import Err, Ok
from threads.store import sql
from threads.store.artifacts import ArtifactStore
from threads.store.losses import LossRow, mark_reported, register_observer, unreported_losses
from threads.store.verify import VerifiedLog, verify_export
from threads.store.worker import Clock, Worker

_KEPT = frozenset({"unsupported_format", "unsupported_critical_event", "branch_not_found"})
"""Read failures reported as they are; every other one is log_corrupt."""


@dataclass(frozen=True, slots=True)
class ChangedBranch:
    """A branch with events past the observer's cursor."""

    branch_id: BranchId
    thread_id: ThreadId
    tenant_id: str
    head_seq: int
    cursor: int


@dataclass(frozen=True, slots=True)
class Checkpoint:
    branch_id: BranchId
    seq: int


def _changed(conn: sqlite3.Connection, observer: str) -> list[ChangedBranch]:
    rows: list[tuple[object, ...]] = conn.execute(
        "SELECT b.branch_id, b.thread_id, b.tenant_id, b.head_seq,"
        "   coalesce(c.seq, b.fork_at_seq, 0) AS cursor"
        " FROM branches b"
        " LEFT JOIN observer_cursors c ON c.branch_id = b.branch_id AND c.observer = ?"
        " WHERE b.state NOT IN ('forking', 'fork_failed')"
        "   AND b.head_seq > coalesce(c.seq, b.fork_at_seq, 0)"
        " ORDER BY b.branch_id",
        (observer,),
    ).fetchall()
    return [
        ChangedBranch(
            BranchId(sql.text_of(b)),
            ThreadId(sql.text_of(t)),
            sql.text_of(tenant),
            sql.int_of(head),
            sql.int_of(cursor),
        )
        for b, t, tenant, head, cursor in rows
    ]


class Feed:
    """One observer's view of every tenant's branches."""

    def __init__(self, worker: Worker, artifacts: ArtifactStore, now: Clock, observer: str) -> None:
        self._worker = worker
        self._artifacts = artifacts
        self.now = now
        self.observer = observer

    async def changed(self) -> list[ChangedBranch]:
        """Every listed branch whose head is past the observer's cursor, in branch id order. A
        branch without a cursor row starts at its fork point, or 0: a new fork never re-reads
        its parent."""
        return await self._worker.call(lambda c: _changed(c, self.observer))

    async def chain(self, branch_id: BranchId) -> Ok[VerifiedLog] | Err[ParseError]:
        """A branch's verified resolved chain, whatever its tenant."""

        def read(conn: sqlite3.Connection) -> Ok[bytes] | Err[ParseError]:
            found = sql.branch(conn, branch_id)
            if found is None:
                return Err(ParseError("branch_not_found", f"no branch {branch_id}"))
            try:
                lines = sql.export(conn, branch_id)
            except LookupError as missing:  # an ancestor segment is gone
                return Err(ParseError("branch_not_found", str(missing)))
            if found.dropped_ref is None:
                return Ok(lines)
            tail = self._artifacts.get(found.dropped_ref)
            return tail if isinstance(tail, Err) else Ok(lines + tail.value)

        data = await self._worker.call(read)
        read_back = data if isinstance(data, Err) else verify_export(data.value, self.now())
        if isinstance(read_back, Err) and read_back.error.code not in _KEPT:
            # Any other verify failure of stored bytes is corruption of what was written.
            e = read_back.error
            return Err(ParseError("log_corrupt", e.message, e.seq))
        return read_back

    async def checkpoint(self, rows: Sequence[Checkpoint]) -> None:
        """Moves each cursor forward, never back, in one transaction."""

        def write(conn: sqlite3.Connection) -> None:
            with sql.transaction(conn):
                for row in rows:
                    conn.execute(
                        "INSERT INTO observer_cursors (observer, branch_id, seq) VALUES (?, ?, ?)"
                        " ON CONFLICT (observer, branch_id)"
                        " DO UPDATE SET seq = max(seq, excluded.seq)",
                        (self.observer, row.branch_id, row.seq),
                    )

        await self._worker.call(write)

    async def register(self) -> None:
        await self._worker.call(lambda c: register_observer(c, self.observer, self.now()))

    async def unreported_losses(self) -> tuple[LossRow, ...]:
        return await self._worker.call(lambda c: unreported_losses(c, self.observer))

    async def mark_reported(self, rows: Sequence[LossRow]) -> None:
        def write(conn: sqlite3.Connection) -> None:
            with sql.transaction(conn):
                mark_reported(conn, self.observer, rows, self.now())

        await self._worker.call(write)
