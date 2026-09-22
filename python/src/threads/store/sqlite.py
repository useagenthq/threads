"""The log store: SQLite holding each canonical line's exact bytes (ADRs 0004-0006, 0008).

Statements run on the store's own thread (`Worker`, ). `SqliteStore.open()`
with the default ":memory:" path is the in-memory store tests use: the same code, no file.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from threads import VERSION
from threads.log import BranchId, ParseError, ThreadId
from threads.log.digest import sha256_hex
from threads.result import Err, Ok
from threads.store import lease, sql
from threads.store.forking import start_child
from threads.store.lines import header_line
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

    def __init__(self, worker: Worker, tenant_id: str) -> None:
        self._worker = worker
        self._tenant = tenant_id

    @classmethod
    async def open(
        cls, path: str | Path = ":memory:", *, tenant_id: str = sql.LOCAL_TENANT
    ) -> Ok["SqliteStore"] | Err[ParseError]:
        """Opens or creates the store, scoped to one tenant: another tenant's branches are
        branch_not_found. A database a newer schema wrote is unsupported_format."""
        worker = await Worker.open(str(path))
        error = await worker.call(sql.install)
        if error is not None:
            await worker.close()
            return Err(error)
        return Ok(cls(worker, tenant_id))

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
        return _result(await self._worker.call(lambda c: lease.create(c, row, (), None)))

    async def import_log(self, log: VerifiedLog) -> Ok[None] | Err[ParseError]:
        """Stores a verified export's lines byte for byte: parents referenced, never copied."""
        tenant = self._tenant
        return _result(await self._worker.call(lambda c: sql.import_segments(c, log, tenant)))

    async def export(self, branch_id: BranchId) -> Ok[bytes] | Err[ParseError]:
        """The JSONL export of a branch, ending with its committed head checkpoint."""
        found = await self._owned(branch_id)
        if isinstance(found, Err):
            return found
        return Ok(await self._worker.call(lambda c: sql.export(c, branch_id)))

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
        log = read.value
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
        """Creates a child at the parent's line `at_seq`: its own header and `fork` event in
        one transaction, with its first lease. Parent rows are referenced, never copied. A
        repair child is inspection-only and gets no writer.

        Fork-point eligibility (semantic rule 16) and the sandbox restore belong to the fork
        operation above the store."""
        now = clock()
        read = await self._prefix(request.parent, request.at_seq, now)
        if isinstance(read, Err):
            return read
        started = start_child(read.value, self._tenant, request.child, request.data, now)
        if isinstance(started, Err):
            return started
        start = started.value
        taken = lease.Lease(holder_id, start.epoch, now + lease.TTL_MS) if start.runnable else None
        error = await self._worker.call(lambda c: lease.create(c, start.row, (start.fork,), taken))
        if error is not None:
            return Err(error)
        if taken is None:
            return Ok(None)
        return Ok(Writer(self._worker, taken, start.fold, start.fork[1], clock))

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


def _major(version: str) -> str:
    return version.split(".", 1)[0]


def _result(error: ParseError | None) -> Ok[None] | Err[ParseError]:
    return Ok(None) if error is None else Err(error)


def _corrupt(cause: ParseError) -> ParseError:
    # A stored branch that fails verification refuses writable opens; the precise code is the
    # cause (spec/schema/README.md wire rule 14). Readers still get the precise error.
    return ParseError("log_corrupt", f"{cause.code}: {cause.message}", cause.seq)
