"""The single writer of one branch: `validate_next`, then the fenced conditional append."""

import asyncio
from collections.abc import Callable, Sequence

from threads.log import BranchId, ParseError
from threads.log.digest import sha256_hex
from threads.redaction import SecretInStoredBytesError, published
from threads.reduce import Fold, apply
from threads.reduce.state import HeadRef, ReducedState, reduced_state
from threads.result import Err, Ok
from threads.store import lease, sql
from threads.store.companion import Companion
from threads.store.lines import Draft, Position, event_line, stored_secret
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
    def owner(self) -> lease.Owner:
        """What fences this writer's resource-ledger rows."""
        return lease.Owner(self._branch, self._lease)

    @property
    def fold(self) -> Fold:
        """The committed branch folded through its last append. Read it; never mutate it."""
        return self._fold

    def state(self) -> ReducedState:
        """ReducedState at the committed head: what hooks are shown."""
        return reduced_state(self._fold, HeadRef(self._fold.seq, sha256_hex(self._last_line)))

    @property
    def requires_recovery(self) -> bool:
        """True when the branch had a call without a result, a model request without a response,
        or an effect in doubt when this writer took it. Normal dispatch must refuse such a
        writer: only recovery may dispatch on it, after re-checking approval,
        cancellation and policy."""
        return self._requires_recovery

    async def append(
        self,
        drafts: Sequence[Draft],
        companion: Companion | None = None,
        *,
        admit: Callable[[Fold, Sequence[Draft]], Sequence[Draft]] | None = None,
    ) -> Ok[tuple[StoredEvent, ...]] | Err[ParseError]:
        """Appends the drafts as one transaction and resolves after it is durable. A companion
        that refuses rolls the append back: its error is the result and the writer goes on.
        `admit` picks the drafts the batch keeps, from the fold as it is under the writer's lock
        (another task's append can land while this one waits for it)."""
        async with self._lock:
            if self._poisoned:
                return Err(ParseError("writer_poisoned", "this writer lost its lease or head"))
            if admit is not None:
                drafts = admit(self._fold, drafts)
            now = self._clock()
            expected = self._fold.seq
            built = self._build(drafts, now)
            if isinstance(built, Err):
                await self._reload(now)
                return built
            rows, content = built.value
            if not rows:
                return Ok(())
            batch = lease.Batch(expected, rows, sha256_hex(rows[-1][1]))
            error = await self._commit(batch, content, now, companion)
            if isinstance(error, lease.Refused):
                await self._reload(now)
                return Err(error.error)
            if error is not None:
                return Err(error)
            return Ok(tuple(event for event, _ in rows))

    async def _commit(
        self, batch: lease.Batch, content: bytes, now: int, companion: Companion | None
    ) -> ParseError | lease.Refused | None:
        """Runs the append to settlement even if the caller is cancelled: the statement can't
        be recalled once queued, and the fold already holds the batch. Until it settles the
        writer is poisoned, so a second cancellation leaves it poisoned, never out of step."""
        self._poisoned = True
        op = asyncio.ensure_future(
            self._worker.call(
                lambda c: _published(
                    content, lambda: lease.append(c, self._lease, now, batch, companion)
                )
            )
        )
        try:
            error = await asyncio.shield(op)
        except asyncio.CancelledError:
            settled = await op
            if settled is None:
                self._settled(batch)
            elif isinstance(settled, lease.Refused):
                # Nothing was written: fold the committed log again, as append does.
                await self._reload(now)
            raise
        if error is None:
            self._settled(batch)
        return error

    def _settled(self, batch: lease.Batch) -> None:
        self._last_line = batch.rows[-1][1]
        self._poisoned = False

    def _build(
        self, drafts: Sequence[Draft], now: int
    ) -> Ok[tuple[list[tuple[StoredEvent, bytes]], bytes]] | Err[ParseError]:
        """The drafts' rows, and their content's canonical bytes (one per line)."""
        rows: list[tuple[StoredEvent, bytes]] = []
        content: list[bytes] = []
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
            event, line, stored = built.value
            rows.append((event, line))
            content.append(stored)
            prev = line
        return Ok((rows, b"\n".join(content)))

    async def _reload(self, now: int) -> None:
        # A rejected draft may leave earlier drafts of its batch folded in; the committed
        # log is the truth, so fold it again.
        export = await self._worker.call(lambda c: sql.export(c, self._branch))
        match verify_export(export, now):
            case Ok(value=log):
                self._fold = log.fold
                self._last_line = log.segments[-1].last_line
                self._poisoned = False
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

    async def release(self) -> None:
        """Hands the lease back so the next executor can take the branch at once, only while
        this holder and epoch still hold it. The writer is done afterwards."""
        now = self._clock()
        await self._worker.call(lambda c: lease.release(c, self._branch, self._lease, now))
        self._poisoned = True

    async def renew(self) -> Ok[None] | Err[ParseError]:
        """Extends the lease. Once lost it stays lost: the writer is poisoned."""
        now = self._clock()
        renewed = await self._worker.call(lambda c: lease.renew(c, self._branch, self._lease, now))
        if isinstance(renewed, ParseError):
            self._poisoned = True
            return Err(renewed)
        self._lease = renewed
        return Ok(None)


def _published(
    content: bytes, append: Callable[[], ParseError | lease.Refused | None]
) -> ParseError | lease.Refused | None:
    """The append, unless a value registered since `event_line` checked it is in the content:
    registration is paused until the rows are durable (C5). Refused, like a companion's
    refusal: nothing was written, so the writer reloads and goes on."""
    try:
        return published(content, append)
    except SecretInStoredBytesError:
        return lease.Refused(stored_secret())
