"""`open_thread` and the `Thread` handle (spec/api.json, ): a thread positioned at
one branch. Every method reads or appends through the store; none needs the agent in memory."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from threads.agents.store import HOLDER, Store, now_ms, open_store
from threads.log import BranchId, Event, EventId, ParseError, SnapshotData, SnapshotEvent, ThreadId
from threads.result import Err, Ok
from threads.sandbox.protocol import Sandbox
from threads.store import VerifiedLog
from threads.store.lines import uuid7
from threads.thread.case import CaseExpectation, CaseRequest, SavedCase, save_case
from threads.thread.fork import ForkAt, KnowledgePolicy, fork_branch

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True, slots=True)
class ForkPoint:
    """host-api ForkPoint: an eligible snapshot event, the value `fork` takes."""

    branch_id: BranchId
    seq: int
    event_id: EventId
    snapshot: SnapshotData


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    event: Event
    fork_point: bool
    """True for an eligible snapshot event."""


@dataclass(frozen=True, slots=True)
class Timeline:
    """host-api Timeline: the resolved chain, oldest first. A model_request's request_ref is
    the exact bytes the model saw."""

    thread_id: ThreadId
    branch_id: BranchId
    entries: tuple[TimelineEntry, ...]


@dataclass(frozen=True, slots=True)
class Thread:
    """A thread positioned at one branch. Pass it back to `run` to continue the thread."""

    id: ThreadId
    branch: BranchId
    store: Store
    sandbox: Sandbox | None = field(default=None, kw_only=True, compare=False, repr=False)
    """The adapter this thread's snapshots restore into: `fork` needs it, and `save_case`
    checks its egress."""

    async def timeline(self) -> Ok[Timeline] | Err[ParseError]:
        """Every step, with the fork points marked (F13.1)."""
        read = await self._read()
        if isinstance(read, Err):
            return read
        points = set(read.value.fold.fork_points)
        entries = tuple(
            TimelineEntry(e, (e.seq, e.event_id) in points) for e in read.value.fold.events
        )
        return Ok(Timeline(self.id, self.branch, entries))

    async def fork_points(self) -> Ok[tuple[ForkPoint, ...]] | Err[ParseError]:
        """Eligible snapshot events only, oldest first."""
        read = await self._read()
        return read if isinstance(read, Err) else Ok(_fork_points(read.value))

    async def fork(
        self, point: EventId | ForkPoint, *, knowledge: KnowledgePolicy = "pinned"
    ) -> Ok["Thread"] | Err[ParseError]:
        """A new branch restored into an isolated sandbox. Continue it with
        `agent.run(input, thread=child)`."""
        event_id = point if isinstance(point, str) else point.event_id
        child = BranchId(uuid7(now_ms()))
        at = ForkAt(self.branch, event_id, child, knowledge)
        sq = await open_store(self.store)
        forked = await fork_branch(sq, self.sandbox, at, HOLDER, now_ms)
        if isinstance(forked, Err):
            return forked
        # Done with the child: hand its lease back so a run (its own holder) takes it at once.
        await forked.value.release()
        return Ok(Thread(self.id, child, self.store, sandbox=self.sandbox))

    async def save_case(
        self,
        name: str,
        *,
        expect: CaseExpectation,
        external_effects: Literal["stub"],
        at: EventId | None = None,
        dir: str = "cases",
    ) -> Ok[SavedCase] | Err[ParseError]:
        """Writes `<dir>/<name>/`: the export through the snapshot, its artifacts, and case.json
        with the assertion and declared dependencies."""
        read = await self._read()
        if isinstance(read, Err):
            return read
        request = CaseRequest(name, expect, external_effects, at, dir)
        return await save_case(await open_store(self.store), read.value, self.sandbox, request)

    async def _read(self) -> Ok[VerifiedLog] | Err[ParseError]:
        sq = await open_store(self.store)
        return await sq.read(self.branch, now_ms())


def _fork_points(log: VerifiedLog) -> tuple[ForkPoint, ...]:
    points = set(log.fold.fork_points)
    snapshots: Sequence[SnapshotEvent] = [
        e for e in log.fold.events if isinstance(e, SnapshotEvent) and (e.seq, e.event_id) in points
    ]
    return tuple(ForkPoint(e.branch_id, e.seq, e.event_id, e.data) for e in snapshots)


async def open_thread(
    store: Store,
    thread_id: ThreadId,
    *,
    branch_id: BranchId | None = None,
    sandbox: Sandbox | None = None,
) -> Ok[Thread] | Err[ParseError]:
    """spec/api.json `openThread`: a handle for inspection and control. The branch defaults to
    the thread's main (root) branch. A branch that fails verification is log_corrupt."""
    sq = await open_store(store)
    branch = await sq.root(thread_id) if branch_id is None else Ok(branch_id)
    if isinstance(branch, Err):
        return branch
    read = await sq.read(branch.value, now_ms())
    if isinstance(read, Err):
        return Err(_open_error(read.error))
    if read.value.fold.thread_id != thread_id:
        return Err(ParseError("not_found", f"no branch {branch.value} in thread {thread_id}"))
    return Ok(Thread(thread_id, branch.value, store, sandbox=sandbox))


def _open_error(error: ParseError) -> ParseError:
    match error.code:
        case "branch_not_found":
            return ParseError("not_found", error.message)
        case "unsupported_format" | "unsupported_critical_event":
            return error
        case _:
            return ParseError("log_corrupt", f"{error.code}: {error.message}", error.seq)
