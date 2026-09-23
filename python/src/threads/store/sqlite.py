"""The log store: SQLite holding each canonical line's exact bytes (ADRs 0004-0006, 0008).

Statements run on the store's own thread (`Worker`, ). `SqliteStore.open()`
with the default ":memory:" path is the in-memory store tests use: the same code, no file.
"""

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from threads import VERSION
from threads.log import BranchId, Event, ParseError, ThreadId
from threads.log.digest import sha256_hex
from threads.render import Rendered, render
from threads.render.verify import verify_requests
from threads.result import Err, Ok
from threads.store import lease, sql
from threads.store.artifacts import ArtifactStore, FileArtifacts, MemoryArtifacts
from threads.store.context import CleanupContext, OwnerContext
from threads.store.forking import Forking, forking, start_child
from threads.store.lines import Draft, head_line, header_line
from threads.store.resources import Ledger, Resource
from threads.store.spill import Spill
from threads.store.verify import VerifiedLog, verify_export
from threads.store.worker import Clock, Worker
from threads.store.writer import Writer


@dataclass(frozen=True, slots=True)
class ForkRequest:
    parent: BranchId
    at_seq: int
    child: BranchId
    data: Mapping[str, JsonValue]
    """The fork payload (reason, sandbox_id, knowledge_policy) minus the derived parent link."""


class SqliteStore:
    """Append-only branches of lines, their head checkpoints and their leases."""

    def __init__(self, worker: Worker, tenant_id: str, artifacts: ArtifactStore) -> None:
        self._worker = worker
        self._tenant = tenant_id
        self._artifacts = artifacts

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

        return _result(await self._worker.call(store))

    async def put_artifact(self, data: bytes) -> str:
        """Stores bytes content-addressed and returns their sha256 once they are durable."""
        return await self._worker.call(lambda _: self._artifacts.put(data))

    async def spill(self) -> Spill:
        """A new artifact written a chunk at a time, never held whole in memory."""
        return Spill(self._worker, await self._worker.call(lambda _: self._artifacts.sink()))

    async def get_artifact(self, sha256: str) -> Ok[bytes] | Err[ParseError]:
        """An artifact's bytes, verified against its hash."""
        return await self._worker.call(lambda _: self._artifacts.get(sha256))

    async def render(
        self, events: Sequence[Event], *, compaction: bool = False
    ) -> Ok[Rendered] | Err[ParseError]:
        """Render v1 of the next request after `events`, reading every artifact it references
        on the store's thread (a missing or changed one is an error, never a substitute)."""
        return await self._worker.call(
            lambda _: render(events, self._artifacts.get, compaction=compaction)
        )

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
        branch, and with writer_mismatch at the header for one that another implementation writes
       ."""
        owned = await self._owned(branch_id)
        if isinstance(owned, Err):
            return owned
        found = owned.value
        if found.state != "ready":
            message = f"branch {branch_id} is {found.state}"
            return Err(ParseError("branch_not_runnable", message, found.head_seq))
        read = await self.read(branch_id, clock())
        if isinstance(read, Err):
            return Err(_corrupt(read.error))
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

    async def fork(
        self, request: ForkRequest, holder_id: str, clock: Clock
    ) -> Ok[Writer | None] | Err[ParseError]:
        """Creates a child at the parent's line `at_seq` with nothing to restore (a repair, or a
        test): `begin_fork` then `finish_fork`. Parent rows are referenced, never copied. A
        repair child is inspection-only and gets no writer."""
        begun = await self.begin_fork(
            request.parent, request.at_seq, request.child, holder_id, clock
        )
        if isinstance(begun, Err):
            return begun
        finished = await self.finish_fork(begun.value, request.data, clock)
        if isinstance(finished, Err):
            await self.fail_fork(request.child)
        return finished

    async def begin_fork(
        self, parent: BranchId, at_seq: int, child: BranchId, holder_id: str, clock: Clock
    ) -> Ok[Forking] | Err[ParseError]:
        """Stores the child as `forking` with its first lease, which fences the ledger rows of
        whatever the fork restores. Fork-point eligibility (semantic rule 16) and the restore
        belong to the fork operation above the store."""
        now = clock()
        read = await self._prefix(parent, at_seq, now)
        if isinstance(read, Err):
            return read
        started = forking(read.value, self._tenant, child, holder_id, now)
        held = started.owner.lease
        error = await self._worker.call(lambda c: lease.create(c, started.row, held))
        return Err(error) if error is not None else Ok(started)

    async def finish_fork(
        self, started: Forking, data: Mapping[str, JsonValue], clock: Clock
    ) -> Ok[Writer | None] | Err[ParseError]:
        """Writes the child's `fork` event and makes it ready (or inspection-only) in one
        transaction, fenced by the fork's lease."""
        built = start_child(started, data, clock())
        if isinstance(built, Err):
            return built
        start, held = built.value, started.owner.lease
        error = await self._worker.call(lambda c: lease.finish_fork(c, start.row, start.fork, held))
        if error is not None:
            return Err(error)
        if not start.runnable:
            return Ok(None)
        return Ok(Writer(self._worker, held, start.fold, start.fork[1], clock))

    async def fail_fork(self, child: BranchId) -> None:
        """The fork failed: the child becomes `fork_failed` and is never listed."""
        await self._worker.call(lambda c: lease.fail_fork(c, child))

    async def interrupted_forks(self, holder_id: str, clock: Clock) -> tuple[lease.Owner, ...]:
        """Forks a crash left `forking`, each taken over under a new lease:
        this holder's own, or any whose lease ran out. A fork is never resumed; the caller
        releases what it created and marks it `fork_failed`."""
        tenant, now = self._tenant, clock()

        def take_over(conn: sqlite3.Connection) -> tuple[lease.Owner, ...]:
            owners: list[lease.Owner] = []
            for child in sql.forking(conn, tenant):
                taken = lease.take(conn, child, holder_id, 0, now)
                if isinstance(taken, lease.Lease):
                    owners.append(lease.Owner(child, taken))
            return tuple(owners)

        return await self._worker.call(take_over)

    async def branch(self, branch_id: BranchId) -> Ok[sql.Branch] | Err[ParseError]:
        """The branch's row, in any state; another tenant's is branch_not_found."""
        return await self._owned(branch_id)

    async def _prefix(
        self, parent: BranchId, at_seq: int, now: int
    ) -> Ok[VerifiedLog] | Err[ParseError]:
        """The parent's verified resolved chain through `at_seq`, where a child forks."""
        owned = await self._owned(parent)
        if isinstance(owned, Err):
            return owned
        prefix = await self._worker.call(lambda c: sql.prefix(c, parent, at_seq))
        read = verify_export(prefix, now)
        if isinstance(read, Err):
            return Err(_corrupt(read.error))
        if read.value.fold.seq != at_seq:
            return Err(ParseError("seq_mismatch", f"the parent has no line {at_seq}", at_seq))
        return read

    async def _owned(self, branch_id: BranchId) -> Ok[sql.Branch] | Err[ParseError]:
        found = await self._worker.call(lambda c: sql.branch(c, branch_id))
        if found is None or found.tenant_id != self._tenant:
            return Err(ParseError("branch_not_found", f"no branch {branch_id}"))
        return Ok(found)


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


def _corrupt(cause: ParseError) -> ParseError:
    # A stored branch that fails verification refuses writable opens; the precise code is the
    # cause (spec/schema/README.md wire rule 14). Readers still get the precise error.
    return ParseError("log_corrupt", f"{cause.code}: {cause.message}", cause.seq)
