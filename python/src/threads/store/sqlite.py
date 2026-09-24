"""The log store: SQLite holding each canonical line's exact bytes (ADRs 0004-0006, 0008).

Statements run on the store's own thread (`Worker`). `SqliteStore.open()`
with the default ":memory:" path is the in-memory store tests use: the same code, no file.
"""

import sqlite3
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from threads import VERSION
from threads.log import BranchId, Event, EventId, ParseError, ThreadId
from threads.log.digest import sha256_hex
from threads.redaction import SecretInStoredBytesError, published
from threads.render import ReadArtifact, Rendered, render
from threads.render.verify import verify_requests
from threads.result import Err, Ok
from threads.store import lease, sql
from threads.store.artifacts import ArtifactStore, FileArtifacts, MemoryArtifacts
from threads.store.bindings import Bindings, Kind
from threads.store.branches import BranchStore, corrupt
from threads.store.budgets import BudgetLedger
from threads.store.context import CleanupContext, OwnerContext
from threads.store.cursors import ObserverCursors
from threads.store.lines import Draft, head_line, header_line, imported_bytes
from threads.store.opening import ALREADY_OPEN, BranchOpening, open_alone
from threads.store.resources import Ledger, Resource
from threads.store.spill import Spill
from threads.store.tables import Tables
from threads.store.verify import VerifiedLog, verify_export
from threads.store.worker import Clock, Worker
from threads.store.writer import Writer

if TYPE_CHECKING:
    from pydantic import JsonValue


class SqliteStore(BranchStore):
    """Append-only branches of lines, their head checkpoints and their leases."""

    @classmethod
    async def open(
        cls,
        path: str | Path = ":memory:",
        *,
        tenant_id: str = sql.LOCAL_TENANT,
        artifacts: ArtifactStore | None = None,
    ) -> Ok["SqliteStore"] | Err[ParseError]:
        """Opens or creates the store, scoped to one tenant: another tenant's branches are
        branch_not_found. A database a newer schema wrote is unsupported_format. Artifacts
        default to `artifacts/` beside the database file (in memory for ":memory:")."""
        worker = await Worker.open(str(path))
        error = await worker.call(sql.install)
        if error is not None:
            await worker.close()
            return Err(error)
        if artifacts is None:
            memory = str(path) == ":memory:"
            artifacts = (
                MemoryArtifacts() if memory else FileArtifacts(Path(path).parent / "artifacts")
            )
        return Ok(cls(worker, tenant_id, artifacts))

    def scoped(self, tenant_id: str) -> "SqliteStore":
        """The same database scoped to another tenant: the host serves each
        authenticated principal's tenant through one of these."""
        return SqliteStore(self._worker, tenant_id, self._artifacts)

    @property
    def tables(self) -> Tables:
        """Approvals, inbox, receipts, schedule claims and branch listing of this tenant."""
        return Tables(self._worker, self._tenant)

    def bindings(self, kind: Kind) -> Bindings:
        """Host-issued memory or knowledge bindings."""
        return Bindings(self._worker, kind)

    async def run[T](
        self, statement: Callable[[sqlite3.Connection], T], *, publishing: bytes | None = None
    ) -> T:
        """A built-in provider's statement on the store's own thread (local_memory,
        local_knowledge keep their tables in the run's store). With `publishing`, those bytes
        are stored as an artifact first, in the same step with registration paused: a
        registered value in them writes nothing (SecretInStoredBytesError, C5)."""
        if publishing is None:
            return await self._worker.call(statement)
        data = publishing

        def both(conn: sqlite3.Connection) -> T:
            self._artifacts.put(data)
            return statement(conn)

        return await self._worker.call(lambda c: published(data, lambda: both(c)))

    @property
    def cursors(self) -> ObserverCursors:
        """Durable observer cursors."""
        return ObserverCursors(self._worker)

    @property
    def budgets(self) -> BudgetLedger:
        """Tree-wide budget reservations."""
        return BudgetLedger(self._worker)

    @property
    def ledger(self) -> Ledger:
        """The resource ledger of this store's tenant."""
        return Ledger(self._worker, self._tenant)

    def context(self, owner: lease.Owner, clock: Clock) -> OwnerContext:
        """The fence for provider operations `owner` dispatches."""
        return OwnerContext(self._worker, owner, clock)

    def cleanup_context(self, row: Resource, clock: Clock) -> CleanupContext:
        """The fence for gc's operations on a row it claimed (`Ledger.claim`)."""
        return CleanupContext(self.ledger, row, clock)

    async def close(self) -> None:
        await self._worker.close()

    async def create(
        self, thread_id: ThreadId, branch_id: BranchId, now: int
    ) -> Ok[None] | Err[ParseError]:
        """Creates a root branch: its header line and nothing else."""
        header = header_line(thread_id, branch_id, now)
        row = sql.Branch(
            branch_id, thread_id, self._tenant, None, None, header, "ready", 0, sha256_hex(header)
        )
        return _result(await self._worker.call(lambda c: lease.create(c, row, None)))

    async def root_or_create(self, thread_id: ThreadId, branch_id: BranchId, now: int) -> BranchId:
        """The thread's root branch, created as `branch_id` if it has none yet (atomic)."""
        header = header_line(thread_id, branch_id, now)
        row = sql.Branch(
            branch_id, thread_id, self._tenant, None, None, header, "ready", 0, sha256_hex(header)
        )
        return await self._worker.call(lambda c: lease.root_or_create(c, row))

    async def open_branch(
        self,
        thread_id: ThreadId,
        branch_id: BranchId,
        drafts: Sequence[Draft],
        *,
        holder_id: str,
        clock: Clock,
    ) -> Ok[Writer | Literal["already_open"]] | Err[ParseError]:
        """`branch.open` in a transaction of its own: a new root branch of this tenant with its
        first events, held by the returned writer at epoch 1. `already_open` when the branch
        exists."""
        now = clock()
        held = lease.Lease(holder_id, 1, now + lease.TTL_MS)
        o = BranchOpening(self._tenant, thread_id, branch_id, held, drafts)
        opened = await self._worker.call(lambda c: open_alone(c, o, now))
        if isinstance(opened, ParseError):
            return Err(opened)
        if isinstance(opened, str):
            return Ok(ALREADY_OPEN)
        return Ok(Writer(self._worker, held, opened.fold, opened.last_line, clock))

    async def import_log(self, log: VerifiedLog) -> Ok[None] | Err[ParseError]:
        """Stores a verified export's lines byte for byte: parents referenced, never copied.
        Every model request must first replay from the log and the artifacts already in the
        store (C7, Render v1): the first failing request's error is the result."""
        tenant, artifacts = self._tenant, self._artifacts

        def store(conn: sqlite3.Connection) -> ParseError | None:
            replayed = verify_requests(log.fold.events, artifacts.get)
            if isinstance(replayed, Err):
                return replayed.error
            # The dropped bytes are durable before the row that references them.
            dropped = artifacts.put(log.dropped) if log.dropped else None
            return sql.import_segments(conn, log, tenant, dropped)

        try:
            # Imported bytes are stored exactly as exported, torn tail included (C5).
            data = imported_bytes(log)
            return _result(await self._worker.call(lambda c: published(data, lambda: store(c))))
        except SecretInStoredBytesError:
            message = "the export holds a registered secret; nothing imported"
            return Err(ParseError("secret_in_stored_bytes", message))

    async def put_artifact(self, data: bytes) -> str:
        """Stores bytes content-addressed and returns their sha256 once they are durable."""
        return await self._worker.call(lambda _: published(data, lambda: self._artifacts.put(data)))

    async def spill(self) -> Spill:
        """A new artifact written a chunk at a time, never held whole in memory."""
        return Spill(self._worker, await self._worker.call(lambda _: self._artifacts.sink()))

    async def get_artifact(self, sha256: str) -> Ok[bytes] | Err[ParseError]:
        """An artifact's bytes, verified against its hash."""
        return await self._worker.call(lambda _: self._artifacts.get(sha256))

    async def render(
        self, events: Sequence[Event], *, compaction: bool = False, cause: EventId | None = None
    ) -> Ok[Rendered] | Err[ParseError]:
        """Render v1 of the next request after `events`, reading every artifact it references
        on the store's thread (a missing or changed one is an error, never a substitute)."""
        return await self.reading(partial(render, events, compaction=compaction, cause=cause))

    async def reading[T](self, job: Callable[[ReadArtifact], T]) -> T:
        return await self._worker.call(lambda _: job(self._artifacts.get))

    async def export(self, branch_id: BranchId) -> Ok[bytes] | Err[ParseError]:
        """The JSONL export of a branch, ending with its committed head checkpoint."""
        found = await self._owned(branch_id)
        if isinstance(found, Err):
            return found
        lines = await self._worker.call(lambda c: sql.export(c, branch_id))
        dropped = found.value.dropped_ref
        if dropped is None:
            return Ok(lines)
        tail = await self._worker.call(lambda _: self._artifacts.get(dropped))
        return tail if isinstance(tail, Err) else Ok(lines + tail.value)

    async def export_through(self, branch_id: BranchId, seq: int) -> Ok[bytes] | Err[ParseError]:
        """The export of a branch's resolved chain through its own line `seq`, with a head
        checkpoint there: what a saved case restores."""
        found = await self._owned(branch_id)
        if isinstance(found, Err):
            return found
        if not (found.value.fork_at_seq or 0) < seq <= found.value.head_seq:
            return Err(ParseError("seq_mismatch", f"branch {branch_id} has no own line {seq}"))
        lines = await self._worker.call(lambda c: sql.prefix(c, branch_id, seq))
        last = lines.removesuffix(b"\n").rsplit(b"\n", 1)[-1]
        return Ok(lines + head_line(branch_id, seq, sha256_hex(last)) + b"\n")

    async def root(self, thread_id: ThreadId) -> Ok[BranchId] | Err[ParseError]:
        """The thread's main branch: its root."""
        found = await self._worker.call(lambda c: sql.root(c, thread_id, self._tenant))
        if found is None:
            return Err(ParseError("not_found", f"no thread {thread_id}"))
        return Ok(found)

    async def read(self, branch_id: BranchId, now: int) -> Ok[VerifiedLog] | Err[ParseError]:
        """Reads a branch back through the same boundary as an import: storage is untrusted."""
        exported = await self.export(branch_id)
        return exported if isinstance(exported, Err) else verify_export(exported.value, now)

    async def acquire(
        self, branch_id: BranchId, holder_id: str, clock: Clock
    ) -> Ok[Writer] | Err[ParseError]:
        """Takes the branch lease at the next epoch. Fails with branch_busy while
        another holder's lease is live, and with branch_not_runnable for an inspection-only
        branch, and with writer_mismatch at the header for one that another implementation
        writes."""
        owned = await self._owned(branch_id)
        if isinstance(owned, Err):
            return owned
        found = owned.value
        if found.state != "ready":
            message = f"branch {branch_id} is {found.state}"
            return Err(ParseError("branch_not_runnable", message, found.head_seq))
        read = await self.read(branch_id, clock())
        if isinstance(read, Err):
            return Err(corrupt(read.error))
        return await self._take(read.value, holder_id, clock)

    async def repair_torn(
        self, branch_id: BranchId, holder_id: str, clock: Clock
    ) -> Ok[Writer] | Err[ParseError]:
        """Takes a torn import (wire rule 14) for its first append: `log_repaired` records where
        the dropped bytes were and their artifact, and then the branch is runnable. A branch
        already repaired is simply acquired; any other inspection-only branch is refused."""
        owned = await self._owned(branch_id)
        if isinstance(owned, Err):
            return owned
        if owned.value.state == "ready":
            return await self.acquire(branch_id, holder_id, clock)
        dropped = owned.value.dropped_ref
        read = await self.read(branch_id, clock())
        if dropped is None or isinstance(read, Err):
            message = f"branch {branch_id} is {owned.value.state}"
            return Err(ParseError("branch_not_runnable", message, owned.value.head_seq))
        taken = await self._take(read.value, holder_id, clock)
        if isinstance(taken, Err):
            return taken
        repaired = await taken.value.append([_repaired(read.value, dropped)])
        if isinstance(repaired, Err):
            return repaired
        await self._worker.call(lambda c: sql.mark_repaired(c, branch_id))
        return taken

    async def _take(
        self, log: VerifiedLog, holder_id: str, clock: Clock
    ) -> Ok[Writer] | Err[ParseError]:
        branch_id = log.segments[-1].header.branch_id
        writer = log.segments[-1].header.writer
        if (writer.impl, _major(writer.version)) != ("threads-py", _major(VERSION)):
            message = "another implementation or major version writes this branch; fork it"
            return Err(ParseError("writer_mismatch", message, 0))
        chain_epoch = log.fold.epoch
        taken = await self._worker.call(
            lambda c: lease.take(c, branch_id, holder_id, chain_epoch, clock())
        )
        if isinstance(taken, ParseError):
            return Err(taken)
        return Ok(Writer(self._worker, taken, log.fold, log.segments[-1].last_line, clock))


def _repaired(log: VerifiedLog, dropped_sha256: str) -> Draft:
    data: dict[str, JsonValue] = {
        "truncated_bytes": len(log.dropped),
        "at_offset": log.committed_bytes,
        "dropped_ref": {
            "sha256": dropped_sha256,
            "bytes": len(log.dropped),
            "media_type": "application/octet-stream",
        },
    }
    return Draft("log_repaired", data, {"kind": "recovery"}, critical=False)


def _major(version: str) -> str:
    return version.split(".", 1)[0]


def _result(error: ParseError | None) -> Ok[None] | Err[ParseError]:
    return Ok(None) if error is None else Err(error)
