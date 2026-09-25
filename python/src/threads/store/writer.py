"""The single writer of one branch: `validate_next`, then the fenced conditional append."""

import asyncio
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass

from threads.log import BranchId, ParseError
from threads.log.digest import sha256_hex
from threads.redaction import SecretInStoredBytesError, published
from threads.reduce import Fold
from threads.reduce.state import HeadRef, ReducedState, reduced_state
from threads.render.artifacts import ReadArtifact
from threads.result import Err, Ok
from threads.store import lease, sql
from threads.store.admit import Admitted, admit, trial
from threads.store.appended import Appended
from threads.store.companion import Companion
from threads.store.conn import CommitUnknownError, Conn
from threads.store.indexing import index_append, known
from threads.store.lines import Draft, Position, stored_secret
from threads.store.verify import StoredEvent
from threads.store.worker import Clock, Worker


@dataclass(frozen=True, slots=True)
class Refusal[E]:
    """A decided append whose decision refused: nothing was appended, and the writer goes on."""

    refusal: E


@dataclass(frozen=True, slots=True)
class DecideTx:
    """What a decided append's decision reads, inside the append's transaction."""

    conn: Conn
    fold: Fold
    """The committed fold the drafts extend: the stored head is its head."""
    now: int
    """The append's clock: every event's time."""
    read: ReadArtifact
    """The store's artifacts, read on its thread: a ref a decision needs (a big reply's text)."""


type Decide[E] = Callable[[DecideTx], Sequence[Draft] | Refusal[E]]
"""Reads the store and builds the drafts, or refuses. It runs on the store's thread and must not
await."""

_REFUSED = ParseError("invalid_request", "the decision refused")
"""Rolls a refused decision back; a Refused never poisons the writer."""

type _Outcome = lease.Batch | ParseError | lease.Refused


@dataclass(frozen=True, slots=True)
class _Commit:
    """One append's commit: where it goes, the trial fold its committed attempt admitted into,
    its clock and companion."""

    head: lease.Head
    fold: Callable[[], Fold]
    now: int
    companion: Companion | None


class Writer:
    """Holds a branch lease at one epoch. Each `append` validates the events against the
    reduced state, then commits them only if the lease is still this writer's and the head is
    where it expects. A lost lease or a moved head poisons the writer."""

    def __init__(  # noqa: PLR0913, PLR0917 - the lease, its fold and head, the clock, the artifacts
        self,
        worker: Worker,
        held: lease.Lease,
        fold: Fold,
        last_line: bytes,
        clock: Clock,
        read: ReadArtifact,
    ) -> None:
        if fold.segment is None or fold.thread_id is None:
            raise ValueError("a writer needs a folded branch")
        self._worker = worker
        self._lease = held
        self._fold = fold
        self._branch: BranchId = fold.segment
        self._last_line = last_line
        self._clock = clock
        self._read = read
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
            chosen = tuple(drafts if admit is None else admit(self._fold, drafts))
            if not chosen:
                return Ok(())

            def fixed(_tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
                return chosen

            outcome, _ = await self._run(fixed, companion)
            return _result(outcome)

    async def append_decided[E](
        self, decide: Decide[E]
    ) -> Ok[tuple[StoredEvent, ...]] | Err[ParseError] | Refusal[E]:
        """One transaction that checks the lease and head, lets `decide` read the store and
        build the drafts, admits them (on the store's thread) and commits. A refusal rolls back
        and leaves the writer usable; `stale_epoch` and `seq_conflict` poison it, as for
        `append`. A batch that comes out empty commits nothing and moves nothing."""
        async with self._lock:
            if self._poisoned:
                return Err(ParseError("writer_poisoned", "this writer lost its lease or head"))
            outcome, refusal = await self._run(decide, None)
            return refusal if refusal is not None else _result(outcome)

    async def _run[E](
        self, decide: Decide[E], companion: Companion | None
    ) -> tuple[_Outcome, Refusal[E] | None]:
        """The append's transaction: the fence, then the decision and the admission on the
        store's thread, into a trial copy of the fold that replaces it once committed."""
        now = self._clock()
        head = lease.Head(self._branch, self._lease, self._fold.seq)
        committed = self._fold
        # One entry per attempt that reached the decision: the store may re-run the whole
        # transaction, so each attempt admits into its own trial of the committed fold, and
        # only the last attempt (the one that committed) counts.
        tries: list[tuple[Fold, Refusal[E] | None]] = []

        def build(conn: Conn) -> lease.Batch | lease.Refused:
            fold = trial(committed)
            decided = decide(DecideTx(conn, committed, now, self._read))
            tries.append((fold, decided if isinstance(decided, Refusal) else None))
            if isinstance(decided, Refusal):
                return lease.Refused(_REFUSED)
            built = self._admit(fold, decided, now)
            if isinstance(built, Err):
                return lease.Refused(built.error)
            return self._batch(head, built.value)

        def last() -> Fold:
            return tries[-1][0] if tries else committed

        outcome = await self._commit(_Commit(head, last, now, companion), build)
        refused = isinstance(outcome, lease.Refused) and len(tries) > 0
        return outcome, tries[-1][1] if refused else None

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
                lambda c: _published(lambda: lease.append(c, head, now, build, after))
            )
        )
        try:
            outcome = await asyncio.shield(op)
        except asyncio.CancelledError:
            self._settle(await op, commit.fold())
            raise
        self._settle(outcome, commit.fold())
        return outcome

    def _settle(self, outcome: _Outcome, fold: Fold) -> None:
        """A committed batch replaces the fold and moves the writer; an empty one or a refusal
        leaves both; a lost lease or head leaves it poisoned."""
        if isinstance(outcome, ParseError):
            return
        self._poisoned = False
        if isinstance(outcome, lease.Batch) and outcome.rows:
            self._fold = fold
            self._last_line = outcome.rows[-1][1]
            self._moved.set()
            self._moved = asyncio.Event()

    def _after(self, commit: _Commit) -> lease.After:
        """The index hooks over the append's events, then its companion."""
        holder, companion = self._lease.holder_id, commit.companion

        def after(conn: Conn, stored: sql.Branch, batch: lease.Batch) -> ParseError | None:
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

    async def fence(self) -> Ok[None] | Err[ParseError]:
        """Checked immediately before anything is dispatched (a model attempt, a tool body, a
        lookup, a termination): a writer whose lease moved on must not reach the adapter, even
        though its intent is already durable. A lost lease poisons the writer."""
        now = self._clock()
        error = await self._worker.read(lambda c: lease.check(c, self._branch, self._lease, now))
        if error is not None:
            self._poisoned = True
            return Err(error)
        return Ok(None)

    async def release(self) -> None:
        """Hands the lease back so the next executor can take the branch at once, only while
        this holder and epoch still hold it. The writer is done afterwards."""
        now = self._clock()
        self._poisoned = True
        await self._worker.call(lambda c: lease.release(c, self._branch, self._lease, now))

    async def renew(self) -> Ok[None] | Err[ParseError]:
        """Extends the lease. Once lost it stays lost: the writer is poisoned."""
        now = self._clock()
        try:
            renewed = await self._worker.call(
                lambda c: lease.renew(c, self._branch, self._lease, now)
            )
        except CommitUnknownError:
            # The renewal may or may not have landed: the owner reloads from the log.
            self._poisoned = True
            raise
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


def _published(append: Callable[[], _Outcome]) -> _Outcome:
    """The append with secret registration paused until its rows are durable (C5): each draft's
    content is checked as it is admitted, inside, so no value registered after that check reaches
    the store."""
    try:
        return published(b"", append)
    except SecretInStoredBytesError:
        return lease.Refused(stored_secret())
