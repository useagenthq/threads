"""The single writer of one branch: `validate_next`, then the fenced conditional append."""

import asyncio
import sqlite3
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass

from threads.log import BranchId, ParseError
from threads.log.digest import sha256_hex
from threads.redaction import SecretInStoredBytesError, published
from threads.reduce import Fold
from threads.reduce.state import HeadRef, ReducedState, reduced_state
from threads.result import Err, Ok
from threads.store import lease, sql
from threads.store.admit import Admitted, admit, trial
from threads.store.appended import Appended
from threads.store.companion import Companion
from threads.store.indexing import index_append, known
from threads.store.lines import Draft, Position, stored_secret
from threads.store.verify import StoredEvent, verify_export
from threads.store.worker import Clock, Worker


@dataclass(frozen=True, slots=True)
class Refusal[E]:
    """A decided append whose decision refused: nothing was appended, and the writer goes on."""

    refused: E


type Decide[E] = Callable[[sqlite3.Connection, Fold], Sequence[Draft] | Refusal[E]]
"""Reads the store inside the append's transaction, with the committed fold the batch extends,
and builds the drafts or refuses. It runs on the store's thread and must not await."""

_REFUSED = ParseError("invalid_request", "the decision refused")
"""Rolls a refused decision back; a Refused never poisons the writer."""

type _Outcome = lease.Batch | ParseError | lease.Refused


@dataclass(frozen=True, slots=True)
class _Commit:
    """One append's commit: where it goes, its content to check, the fold its batch is admitted
    into (the committed one, or a decided append's trial copy), its clock and companion."""

    head: lease.Head
    content: bytes | Sequence[bytes]
    fold: Fold
    now: int
    companion: Companion | None


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
        self._moved = asyncio.Event()
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
        (another task's append can land while this one waits for it). The result is what was
        appended; `Runtime.append_with` reports a batch `admit` changed as `Barred`."""
        async with self._lock:
            if self._poisoned:
                return Err(ParseError("writer_poisoned", "this writer lost its lease or head"))
            if admit is not None:
                drafts = admit(self._fold, drafts)
            now = self._clock()
            head = lease.Head(self._branch, self._lease, self._fold.seq)
            built = self._admit(self._fold, drafts, now)
            if isinstance(built, Err):
                await self._reload(now)
                return built
            if not built.value.rows:
                return Ok(())
            batch = self._batch(head, built.value)
            commit = _Commit(head, built.value.content, self._fold, now, companion)
            return _result(await self._commit(commit, lambda _: batch))

    async def append_decided[E](
        self, decide: Decide[E]
    ) -> Ok[tuple[StoredEvent, ...]] | Err[ParseError] | Refusal[E]:
        """One transaction that checks the lease and head, lets `decide` read the store and
        build the drafts, admits them (on the store's thread) and commits. A refusal rolls back
        and leaves the writer usable; `stale_epoch` and `seq_conflict` poison it, as for
        `append`."""
        async with self._lock:
            if self._poisoned:
                return Err(ParseError("writer_poisoned", "this writer lost its lease or head"))
            now = self._clock()
            head = lease.Head(self._branch, self._lease, self._fold.seq)
            committed, fold = self._fold, trial(self._fold)
            refusals: list[Refusal[E]] = []

            def build(conn: sqlite3.Connection) -> lease.Batch | lease.Refused:
                decided = decide(conn, committed)
                if isinstance(decided, Refusal):
                    refusals.append(decided)
                    return lease.Refused(_REFUSED)
                built = self._admit(fold, decided, now)
                if isinstance(built, Err):
                    return lease.Refused(built.error)
                return self._batch(head, built.value)

            outcome = await self._commit(_Commit(head, (), fold, now, None), build)
            return refusals[0] if refusals else _result(outcome)

    def _admit(
        self, fold: Fold, drafts: Sequence[Draft], now: int
    ) -> Ok[Admitted] | Err[ParseError]:
        thread = fold.thread_id
        if thread is None:
            raise ValueError("a writer needs a folded branch")
        at = Position(thread, self._branch, fold.seq + 1, self.epoch, self._last_line, now)
        return admit(fold, drafts, at)

    def _batch(self, head: lease.Head, admitted: Admitted) -> lease.Batch:
        rows = admitted.rows
        last = rows[-1][1] if rows else self._last_line
        return lease.Batch(head.expected_seq, rows, sha256_hex(last), admitted.opened)

    async def _commit(self, commit: _Commit, build: lease.Build) -> _Outcome:
        """Runs the append, its decision included, to settlement even if the caller is
        cancelled: the statement can't be recalled once queued. Until it settles the writer is
        poisoned, so a second cancellation leaves it poisoned, never out of step."""
        self._poisoned = True
        head, now, after = commit.head, commit.now, self._after(commit)
        op = asyncio.ensure_future(
            self._worker.call(
                lambda c: _published(
                    commit.content, lambda: lease.append(c, head, now, build, after)
                )
            )
        )
        try:
            outcome = await asyncio.shield(op)
        except asyncio.CancelledError:
            await self._settle(await op, commit.fold, now)
            raise
        await self._settle(outcome, commit.fold, now)
        return outcome

    async def _settle(self, outcome: _Outcome, fold: Fold, now: int) -> None:
        if isinstance(outcome, lease.Batch):
            self._fold = fold
            if outcome.rows:
                self._last_line = outcome.rows[-1][1]
            self._poisoned = False
            self._moved.set()
            self._moved = asyncio.Event()
        elif isinstance(outcome, lease.Refused) and fold is self._fold:
            # Nothing was written, but the batch was folded into the committed fold: fold the
            # committed log again.
            await self._reload(now)
        elif isinstance(outcome, lease.Refused):
            self._poisoned = False

    def _after(self, commit: _Commit) -> lease.After:
        """The index hooks over the append's events, then its companion."""
        holder, companion = self._lease.holder_id, commit.companion

        def after(
            conn: sqlite3.Connection, stored: sql.Branch, batch: lease.Batch
        ) -> ParseError | None:
            events = [event for event, _ in batch.rows]
            appended = Appended(
                stored.tenant_id,
                stored.thread_id,
                self._branch,
                known(events),
                batch.opened,
                holder,
                commit.now,
            )
            indexed = index_append(conn, appended)
            if indexed is not None or companion is None:
                return indexed
            return companion(conn, events)

        return after

    def moved(self) -> Coroutine[None, None, bool]:
        """Returns on this writer's next committed append after this call, whoever made it (a
        control included)."""
        return self._moved.wait()

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


def _result(outcome: _Outcome) -> Ok[tuple[StoredEvent, ...]] | Err[ParseError]:
    if isinstance(outcome, lease.Refused):
        return Err(outcome.error)
    if isinstance(outcome, ParseError):
        return Err(outcome)
    return Ok(tuple(event for event, _ in outcome.rows))


def _published(content: bytes | Sequence[bytes], append: Callable[[], _Outcome]) -> _Outcome:
    """The append, unless a value registered since `event_line` checked it is in the content:
    registration is paused until the rows are durable (C5). Refused, like a companion's
    refusal: nothing was written, so the writer reloads and goes on. A decided append's drafts
    are built inside, with registration already paused."""
    try:
        return published(content, append)
    except SecretInStoredBytesError:
        return lease.Refused(stored_secret())
