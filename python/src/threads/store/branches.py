"""The store's branches and forks: each branch's row, and the fork steps that create a child
branch. `SqliteStore` is this plus the log's reads, writes and artifacts."""

import sqlite3
from collections.abc import Mapping

from pydantic import JsonValue

from threads.log import BranchId, ParseError
from threads.result import Err, Ok
from threads.store import lease, sql
from threads.store.artifacts import ArtifactStore
from threads.store.forking import Forking, ForkRequest, forking, publish_fork, start_child
from threads.store.verify import VerifiedLog, verify_export
from threads.store.worker import Clock, Worker
from threads.store.writer import Writer


class BranchStore:
    """One tenant's view of the store's branches: another tenant's are branch_not_found."""

    def __init__(self, worker: Worker, tenant_id: str, artifacts: ArtifactStore) -> None:
        self._worker = worker
        self._tenant = tenant_id
        self._artifacts = artifacts

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
        error = await self._worker.call(lambda c: publish_fork(c, start, held))
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
            return Err(corrupt(read.error))
        if read.value.fold.seq != at_seq:
            return Err(ParseError("seq_mismatch", f"the parent has no line {at_seq}", at_seq))
        return read

    async def _owned(self, branch_id: BranchId) -> Ok[sql.Branch] | Err[ParseError]:
        found = await self._worker.call(lambda c: sql.branch(c, branch_id))
        if found is None or found.tenant_id != self._tenant:
            return Err(ParseError("branch_not_found", f"no branch {branch_id}"))
        return Ok(found)


def corrupt(cause: ParseError) -> ParseError:
    # A stored branch that fails verification refuses writable opens; the precise code is the
    # cause (spec/schema/README.md wire rule 14). Readers still get the precise error.
    return ParseError("log_corrupt", f"{cause.code}: {cause.message}", cause.seq)
