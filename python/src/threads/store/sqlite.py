"""The log store: SQLite holding each canonical line's exact bytes (ADRs 0004-0006, 0008).

Statements run on the store's own thread (`Worker`). `SqliteStore.open()`
with the default ":memory:" path is the in-memory store tests use: the same code, no file.
"""

import sqlite3
from collections.abc import AsyncIterable, Callable, Sequence
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from threads.log import BranchId, Event, EventId, ParseError, ThreadId
from threads.log.digest import sha256_hex
from threads.redaction import SecretInStoredBytesError, published
from threads.render import ReadArtifact, Rendered, render
from threads.render.verify import verify_requests
from threads.result import Err, Ok
from threads.store import lease, sql, sqlite_driver
from threads.store._feed import Feed
from threads.store.artifacts import ArtifactSink, ArtifactStore, FileArtifacts, MemoryArtifacts
from threads.store.bindings import Bindings, Kind
from threads.store.branches import BranchStore, corrupt
from threads.store.budgets import BudgetLedger
from threads.store.conn import Conn, Dialect, SqliteConn
from threads.store.context import CleanupContext, OwnerContext
from threads.store.cursors import ObserverCursors
from threads.store.lines import Draft, head_line, header_line, imported_bytes
from threads.store.opening import ALREADY_OPEN, BranchOpening, open_alone, open_checked
from threads.store.resources import Ledger, Resource
from threads.store.spill import Spill
from threads.store.tables import Tables
from threads.store.taking import repair_draft, take
from threads.store.verify import VerifiedLog, verify_export
from threads.store.worker import Clock, Worker
from threads.store.writer import Writer
from threads.team.imported import import_indexed

if TYPE_CHECKING:
    from threads.sandbox.tree.tar import ArchiveInvalid, StoredTree


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
        error = await worker.free(sqlite_driver.install)
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

    @property
    def dialect(self) -> Dialect:
        """The engine under this store: "sqlite" or "postgres"."""
        return self._worker.dialect

    async def run[T](
        self,
        statement: Callable[[Conn], T],
        *,
        read_only: bool = False,
        publishing: bytes | None = None,
    ) -> T:
        """A statement on the store's own thread, in one transaction (read-only when asked).
        With `publishing`, those bytes are stored as an artifact first, in the same step with
        registration paused: a registered value in them writes nothing (SecretInStoredBytesError,
        C5)."""
        if read_only:
            return await self._worker.read(statement)
        return await self._worker.call(_stored_first(self._artifacts, publishing, statement))

    def feed(self, observer: str, now: Clock) -> Feed:
        """What a telemetry exporter reads every tenant's branches through (never appends)."""
        return Feed(self._worker, self._artifacts, now, observer)

    async def run_sqlite[T](
        self, statement: Callable[[sqlite3.Connection], T], *, publishing: bytes | None = None
    ) -> T:
        """A statement on the SQLite connection itself, for the built-in providers whose FTS5
        tables live in the run's store (local_memory, local_knowledge): they issue their own
        transactions. They refuse a Postgres store at setup, so this never runs on one."""

        def raw(conn: Conn) -> T:
            if not isinstance(conn, SqliteConn):
                raise TypeError("a SQLite-only provider ran on a Postgres store")
            return statement(conn.raw)

        return await self._worker.free(_stored_first(self._artifacts, publishing, raw))

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
        # Registration is paused until the rows are durable: the drafts are checked inside (C5).
        opened = await self._worker.call(lambda c: published(b"", lambda: open_alone(c, o, now)))
        if isinstance(opened, ParseError):
            return Err(opened)
        if isinstance(opened, str):
            return Ok(ALREADY_OPEN)
        return Ok(
            Writer(self._worker, held, opened.fold, opened.last_line, clock, self._artifacts.get)
        )

    async def open_checked(
        self,
        decide: Callable[[Conn, int], BranchOpening | None],
        clock: Clock,
    ) -> Ok[Writer | Literal["already_open"] | None] | Err[ParseError]:
        """`branch.open` after a check in the same transaction: `decide` reads the store and
        returns the opening (its tenant is this store's), or None to commit nothing."""
        now = clock()

        def scoped(conn: Conn, at: int) -> BranchOpening | None:
            o = decide(conn, at)
            return None if o is None else replace(o, tenant_id=self._tenant)

        opened = await self._worker.call(
            lambda c: published(b"", lambda: open_checked(c, scoped, now))
        )
        if isinstance(opened, ParseError):
            return Err(opened)
        if opened is None:
            return Ok(None)
        if isinstance(opened, str):
            return Ok(ALREADY_OPEN)
        return Ok(
            Writer(
                self._worker,
                opened.lease,
                opened.fold,
                opened.last_line,
                clock,
                self._artifacts.get,
            )
        )

    async def import_log(self, log: VerifiedLog) -> Ok[None] | Err[ParseError]:
        """Stores a verified export's lines byte for byte: parents referenced, never copied.
        Every model request must first replay from the log and the artifacts already in the
        store (C7, Render v1): the first failing request's error is the result. The index rows
        the log holds (wake rows, its teams) are folded again in the same transaction."""
        tenant, artifacts = self._tenant, self._artifacts

        def store(conn: Conn) -> ParseError | None:
            replayed = verify_requests(log.fold.events, artifacts.get)
            if isinstance(replayed, Err):
                return replayed.error
            # The dropped bytes are durable before the row that references them.
            dropped = artifacts.put(log.dropped) if log.dropped else None
            return import_indexed(conn, log, tenant, dropped)

        try:
            # Imported bytes are stored exactly as exported, torn tail included (C5).
            data = imported_bytes(log)
            return _result(await self._worker.call(lambda c: published(data, lambda: store(c))))
        except SecretInStoredBytesError:
            message = "the export holds a registered secret; nothing imported"
            return Err(ParseError("secret_in_stored_bytes", message))

    async def put_artifact(self, data: bytes) -> str:
        """Stores bytes content-addressed and returns their sha256 once they are durable."""
        return await self._worker.free(lambda _: published(data, lambda: self._artifacts.put(data)))

    async def spill(self) -> Spill:
        """A new artifact written a chunk at a time, never held whole in memory."""
        return Spill(self._worker, await self._worker.free(lambda _: self._artifacts.sink()))

    async def put_tree(self, tar: AsyncIterable[bytes]) -> "Ok[StoredTree] | Err[ArchiveInvalid]":
        """Reads an untrusted archive into artifacts, its files and then its tree. The reader
        is async and the sinks are synchronous: every sink call runs on the store's thread, as
        the other artifact writes do. Tree bytes skip the redacting Spill: redaction would
        change their hashes (workspace inputs refuse registered secrets instead)."""
        # threads.sandbox imports the store (its protocol names the store's contexts).
        from threads.sandbox.tree.tar import store_tar  # noqa: PLC0415 - an import cycle

        return await store_tar(tar, self._artifacts, run=self.offload)

    def artifact_sink(self) -> ArtifactSink:
        """A new synchronous artifact sink: open, write and commit it only through `offload`
        (the tree reader's `run`), never on the event loop."""
        return self._artifacts.sink()

    async def offload[T](self, job: Callable[[], T]) -> T:
        """`job` on the store's thread: how an async caller drives a synchronous artifact
        sink without blocking the event loop."""
        return await self._worker.free(lambda _: job())

    async def sweep_artifacts(self, keep: frozenset[str], older_than: int) -> tuple[str, ...]:
        """gc's sweep: the artifacts not in `keep` stored before `older_than` (epoch ms)."""
        return await self._worker.free(lambda _: self._artifacts.sweep(keep, older_than))

    async def get_artifact(self, sha256: str) -> Ok[bytes] | Err[ParseError]:
        """An artifact's bytes, verified against its hash."""
        return await self._worker.free(lambda _: self._artifacts.get(sha256))

    async def render(
        self, events: Sequence[Event], *, compaction: bool = False, cause: EventId | None = None
    ) -> Ok[Rendered] | Err[ParseError]:
        """Render v1 of the next request after `events`, reading every artifact it references
        on the store's thread (a missing or changed one is an error, never a substitute)."""
        return await self.reading(partial(render, events, compaction=compaction, cause=cause))

    async def reading[T](self, job: Callable[[ReadArtifact], T]) -> T:
        return await self._worker.free(lambda _: job(self._artifacts.get))

    async def export(self, branch_id: BranchId) -> Ok[bytes] | Err[ParseError]:
        """The JSONL export of a branch, ending with its committed head checkpoint."""
        found = await self._owned(branch_id)
        if isinstance(found, Err):
            return found
        lines = await self._worker.read(lambda c: sql.export(c, branch_id))
        dropped = found.value.dropped_ref
        if dropped is None:
            return Ok(lines)
        tail = await self._worker.free(lambda _: self._artifacts.get(dropped))
        return tail if isinstance(tail, Err) else Ok(lines + tail.value)

    async def export_through(self, branch_id: BranchId, seq: int) -> Ok[bytes] | Err[ParseError]:
        """The export of a branch's resolved chain through its own line `seq`, with a head
        checkpoint there: what a saved case restores."""
        found = await self._owned(branch_id)
        if isinstance(found, Err):
            return found
        if not (found.value.fork_at_seq or 0) < seq <= found.value.head_seq:
            return Err(ParseError("seq_mismatch", f"branch {branch_id} has no own line {seq}"))
        lines = await self._worker.read(lambda c: sql.prefix(c, branch_id, seq))
        last = lines.removesuffix(b"\n").rsplit(b"\n", 1)[-1]
        return Ok(lines + head_line(branch_id, seq, sha256_hex(last)) + b"\n")

    async def root(self, thread_id: ThreadId) -> Ok[BranchId] | Err[ParseError]:
        """The thread's main branch: its root."""
        found = await self._worker.read(lambda c: sql.root(c, thread_id, self._tenant))
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
        return await take(self._worker, self._artifacts, read.value, holder_id, clock)

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
        taken = await take(self._worker, self._artifacts, read.value, holder_id, clock)
        if isinstance(taken, Err):
            return taken
        repaired = await taken.value.append([repair_draft(read.value, dropped)])
        if isinstance(repaired, Err):
            return repaired
        await self._worker.call(lambda c: sql.mark_repaired(c, branch_id))
        return taken


def _stored_first[T](
    artifacts: ArtifactStore, data: bytes | None, job: Callable[[Conn], T]
) -> Callable[[Conn], T]:
    """`job`, after `data` (if any) is stored as an artifact with registration paused."""
    if data is None:
        return job
    stored = data

    def both(conn: Conn) -> T:
        artifacts.put(stored)
        return job(conn)

    return lambda c: published(stored, lambda: both(c))


def _result(error: ParseError | None) -> Ok[None] | Err[ParseError]:
    return Ok(None) if error is None else Err(error)
