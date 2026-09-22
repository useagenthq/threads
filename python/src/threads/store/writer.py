"""The single writer of one branch: `validate_next`, then the fenced conditional append."""

import asyncio
from collections.abc import Sequence

from threads.log import BranchId, ParseError
from threads.log.digest import sha256_hex
from threads.reduce import Fold, apply
from threads.result import Err, Ok
from threads.store import lease, sql
from threads.store.lines import Draft, Position, event_line
from threads.store.verify import StoredEvent, verify_export
from threads.store.worker import Clock, Worker


class Writer:
    """Holds a branch lease at one epoch. Each `append` validates the events against the
    reduced state, then commits them only if the lease is still this writer's and the head is
    where it expects. A lost lease or a moved head poisons the writer."""

    def __init__(
        self, worker: Worker, held: lease.Lease, fold: Fold, last_line: bytes, clock: Clock
    ) -> None:
        if fold.segment is None or fold.thread_id is None:
            raise ValueError("a writer needs a folded branch")
        self._worker = worker
        self._lease = held
        self._fold = fold
        self._branch: BranchId = fold.segment
        self._last_line = last_line
        self._clock = clock
        self._poisoned = False
        self._lock = asyncio.Lock()
        effects = fold.effects.values()
        in_doubt = any(s == "unknown" for _, s in effects)
        self._requires_recovery = bool(fold.pending or fold.open_requests) or in_doubt

    @property
    def branch_id(self) -> BranchId:
        return self._branch

    @property
    def epoch(self) -> int:
        return self._lease.epoch

    @property
    def fold(self) -> Fold:
        """The committed branch folded through its last append. Read it; never mutate it."""
        return self._fold

    @property
    def requires_recovery(self) -> bool:
        """True when the branch had a call without a result, a model request without a response,
        or an effect in doubt when this writer took it. Normal dispatch must refuse such a
        writer: only recovery may dispatch on it, after re-checking approval,
        cancellation and policy."""
        return self._requires_recovery

    async def append(
        self, drafts: Sequence[Draft]
    ) -> Ok[tuple[StoredEvent, ...]] | Err[ParseError]:
        """Appends the drafts as one transaction and resolves after it is durable."""
        async with self._lock:
            if self._poisoned:
                return Err(ParseError("writer_poisoned", "this writer lost its lease or head"))
            now = self._clock()
            expected = self._fold.seq
            built = self._build(drafts, now)
            if isinstance(built, Err):
                await self._reload(now)
                return built
            rows = built.value
            if not rows:
                return Ok(())
            batch = lease.Batch(expected, rows, sha256_hex(rows[-1][1]))
            error = await self._commit(batch, now)
            if error is not None:
                return Err(error)
            return Ok(tuple(event for event, _ in rows))

    async def _commit(self, batch: lease.Batch, now: int) -> ParseError | None:
        """Runs the append to settlement even if the caller is cancelled: the statement can't
        be recalled once queued, and the fold already holds the batch. Until it settles the
        writer is poisoned, so a second cancellation leaves it poisoned, never out of step."""
        self._poisoned = True
        op = asyncio.ensure_future(
            self._worker.call(lambda c: lease.append(c, self._lease, now, batch))
        )
        try:
            error = await asyncio.shield(op)
        except asyncio.CancelledError:
            if await op is None:
                self._settled(batch)
            raise
        if error is None:
            self._settled(batch)
        return error

    def _settled(self, batch: lease.Batch) -> None:
        self._last_line = batch.rows[-1][1]
        self._poisoned = False

    def _build(
        self, drafts: Sequence[Draft], now: int
    ) -> Ok[list[tuple[StoredEvent, bytes]]] | Err[ParseError]:
        rows: list[tuple[StoredEvent, bytes]] = []
        prev = self._last_line
        thread = self._fold.thread_id
        if thread is None:
            raise ValueError("a writer needs a folded branch")
        for draft in drafts:
            at = Position(thread, self._branch, self._fold.seq + 1, self.epoch, prev, now)
            built = event_line(draft, at)
            if isinstance(built, Err):
                return built
            error = apply(self._fold, built.value[0])
            if error is not None:
                return Err(error)
            rows.append(built.value)
            prev = built.value[1]
        return Ok(rows)

    async def _reload(self, now: int) -> None:
        # A rejected draft may leave earlier drafts of its batch folded in; the committed
        # log is the truth, so fold it again.
        export = await self._worker.call(lambda c: sql.export(c, self._branch))
        match verify_export(export, now):
            case Ok(value=log):
                self._fold = log.fold
                self._last_line = log.segments[-1].last_line
            case Err():
                self._poisoned = True

    async def fence(self) -> Ok[None] | Err[ParseError]:
        """Checked immediately before anything is dispatched (a model attempt, a tool body, a
        lookup, a termination): a writer whose lease moved on must not reach the adapter, even
        though its intent is already durable. A lost lease poisons the writer."""
        now = self._clock()
        error = await self._worker.call(lambda c: lease.check(c, self._branch, self._lease, now))
        if error is not None:
            self._poisoned = True
            return Err(error)
        return Ok(None)

    async def renew(self) -> Ok[None] | Err[ParseError]:
        """Extends the lease. Once lost it stays lost: the writer is poisoned."""
        now = self._clock()
        renewed = await self._worker.call(lambda c: lease.renew(c, self._branch, self._lease, now))
        if isinstance(renewed, ParseError):
            self._poisoned = True
            return Err(renewed)
        self._lease = renewed
        return Ok(None)
