"""The log store: SQLite holding each canonical line's exact bytes (ADRs 0004-0006, 0008).

Statements run on the store's own thread (`Worker`, ). `SqliteStore.open()`
with the default ":memory:" path is the in-memory store tests use: the same code, no file.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

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

    def __init__(self, worker: Worker) -> None:
        self._worker = worker

    @classmethod
    async def open(cls, path: str | Path = ":memory:") -> "SqliteStore":
        return cls(await Worker.open(str(path)))

    async def close(self) -> None:
        await self._worker.close()

    async def create(
        self, thread_id: ThreadId, branch_id: BranchId, now: int
    ) -> Ok[None] | Err[ParseError]:
        """Creates a root branch: its header line and nothing else."""
        header = header_line(thread_id, branch_id, now)
        row = sql.Branch(branch_id, thread_id, None, None, header, "ready", 0, sha256_hex(header))
        return _result(await self._worker.call(lambda c: lease.create(c, row, (), None)))

    async def import_log(self, log: VerifiedLog) -> Ok[None] | Err[ParseError]:
        """Stores a verified export's lines byte for byte: parents referenced, never copied."""
        return _result(await self._worker.call(lambda c: sql.import_segments(c, log)))

    async def export(self, branch_id: BranchId) -> bytes:
        """The JSONL export of a branch, ending with its committed head checkpoint."""
        return await self._worker.call(lambda c: sql.export(c, branch_id))

    async def read(self, branch_id: BranchId, now: int) -> Ok[VerifiedLog] | Err[ParseError]:
        """Reads a branch back through the same boundary as an import: storage is untrusted."""
        return verify_export(await self.export(branch_id), now)

    async def acquire(
        self, branch_id: BranchId, holder_id: str, clock: Clock
    ) -> Ok[Writer] | Err[ParseError]:
        """Takes the branch lease at the next epoch. Fails with branch_busy while
        another holder's lease is live, and with branch_not_runnable for an inspection-only
        branch or one that another implementation writes."""
        found = await self._worker.call(lambda c: sql.branch(c, branch_id))
        if found is None:
            raise LookupError(f"no branch {branch_id}")
        if found.state != "ready":
            message = f"branch {branch_id} is {found.state}"
            return Err(ParseError("branch_not_runnable", message, found.head_seq))
        read = await self.read(branch_id, clock())
        if isinstance(read, Err):
            # Corruption refuses writable opens; readers still get the error and its seq.
            return Err(ParseError("log_corrupt", read.error.message, read.error.seq))
        log = read.value
        if log.segments[-1].header.writer.impl != "threads-py":
            message = "another implementation writes this branch; continue on a fork"
            return Err(ParseError("branch_not_runnable", message, log.head.seq))
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
        prefix = await self._worker.call(lambda c: sql.prefix(c, request.parent, request.at_seq))
        read = verify_export(prefix, now)
        if isinstance(read, Err):
            return Err(ParseError("log_corrupt", read.error.message, read.error.seq))
        if read.value.fold.seq != request.at_seq:
            message = f"the parent has no line {request.at_seq}"
            return Err(ParseError("seq_mismatch", message, request.at_seq))
        started = start_child(read.value, request.child, request.data, now)
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


def _result(error: ParseError | None) -> Ok[None] | Err[ParseError]:
    return Ok(None) if error is None else Err(error)
