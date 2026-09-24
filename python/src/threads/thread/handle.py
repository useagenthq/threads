"""`open_thread` and the `Thread` handle (spec/api.json): a thread positioned at
one branch. Every method reads or appends through the store; none needs the agent in memory."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Literal

from threads._generated.host_api_v1 import (
    BranchInfo,
    PendingApproval,
    SettingsChange,
)
from threads.adapters.loop_resources import holding
from threads.agents.store import HOLDER, Store, now_ms, open_store
from threads.log import (
    AgentFinishedEvent,
    AgentSpawnedEvent,
    BranchId,
    CacheBreak,
    CallId,
    Cost,
    Event,
    EventId,
    ParseError,
    PermissionMode,
    PermissionRule,
    Principal,
    SnapshotData,
    SnapshotEvent,
    ThreadId,
    Todo,
    TodosUpdatedEvent,
    UsageTotals,
)
from threads.loop import defaults
from threads.loop.stubs import Stub, parse_stubs
from threads.reduce.projections import cache_breaks, cost
from threads.reduce.state import usage_totals
from threads.render.verify import verify_requests
from threads.result import Err, Ok
from threads.sandbox.protocol import Sandbox
from threads.store import VerifiedLog
from threads.store.lines import uuid7
from threads.thread import approvals, control, style, tree
from threads.thread.authority import Checked, refused
from threads.thread.case import (
    CaseExpectation,
    CaseRequest,
    SavedCase,
    recorded_stubs,
    save_case,
)
from threads.thread.control import Controlled
from threads.thread.fork import ForkAt, KnowledgePolicy, fork_branch, fork_point
from threads.thread.member_view import member_of, with_member
from threads.thread.read import read_error, read_log
from threads.thread.usage import tree_cost


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
class Child:
    """host-api Child: a child thread and its terminal status, or running."""

    child_thread_id: ThreadId
    status: Literal["running", "completed", "failed", "cancelled", "budget_exhausted"]


@dataclass(frozen=True, slots=True)
class Thread:
    """A thread positioned at one branch. Pass it back to `run` to continue the thread."""

    id: ThreadId
    branch: BranchId
    store: Store
    sandbox: Sandbox | None = field(default=None, kw_only=True, compare=False, repr=False)
    """The adapter this thread's snapshots restore into: `fork` needs it, and `save_case`
    checks its egress."""
    authority: Checked | None = field(default=None, kw_only=True, compare=False, repr=False)
    """Who may answer approvals and resolve parked effects: a host handle's approval authority,
    checked as it stands now; None, in-process operator authority (threads.thread.authority)."""
    stubs: tuple[Stub, ...] | None = field(default=None, kw_only=True, compare=False, repr=False)
    """Stub mode: a run of this handle answers every mediated operation from these,
    never live. None: live."""

    async def timeline(self) -> Ok[Timeline] | Err[ParseError]:
        """Every step, with the fork points marked (F13.1)."""
        read = await read_log(self.store, self.branch)
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
        self,
        point: EventId | ForkPoint,
        *,
        mode: Literal["live", "stub"] = "live",
        knowledge: KnowledgePolicy = "pinned",
    ) -> Ok["Thread"] | Err[ParseError]:
        """A new branch restored into an isolated sandbox. Continue it with
        `agent.run(input, thread=child)`. In stub mode that run answers every mediated
        operation from what this branch recorded after the point; it needs a sandbox
        that enforces deny-all egress."""
        event_id = point if isinstance(point, str) else point.event_id
        stubs = None
        if mode == "stub":
            recorded = await self._recorded_after(event_id)
            if isinstance(recorded, Err):
                return recorded
            stubs = recorded.value
        child = BranchId(uuid7(now_ms()))
        at = ForkAt(self.branch, event_id, child, knowledge)
        sq = await open_store(self.store)
        async with holding():  # the restore's sandbox connections are closed when it returns
            forked = await fork_branch(sq, self.sandbox, at, HOLDER, now_ms)
        if isinstance(forked, Err):
            return forked
        # Done with the child: hand its lease back so a run (its own holder) takes it at once.
        await forked.value.release()
        return Ok(Thread(self.id, child, self.store, sandbox=self.sandbox, stubs=stubs))

    async def _recorded_after(self, point: EventId) -> Ok[tuple[Stub, ...]] | Err[ParseError]:
        """The stubs a stub fork at `point` replays: this branch's recorded results after it."""
        if self.sandbox is not None and self.sandbox.info.egress != "enforced":
            why = f"{self.sandbox.info.provider} can't enforce deny-all egress"
            return Err(ParseError("egress_policy_unsupported", why))
        read = await self._read()
        if isinstance(read, Err):
            return read
        at = fork_point(read.value.fold, point)
        if isinstance(at, Err):
            return at
        return Ok(parse_stubs({"stubs": recorded_stubs(read.value.fold, at.value.seq)}))

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

    async def todos(self) -> Ok[tuple[Todo, ...]] | Err[ParseError]:
        """The agent's current todo list: the latest todos_updated."""
        read = await self._read()
        if isinstance(read, Err):
            return read
        events = read.value.fold.events
        latest = next((e for e in reversed(events) if isinstance(e, TodosUpdatedEvent)), None)
        return Ok(() if latest is None else tuple(latest.data.todos))

    async def children(self) -> Ok[tuple[Child, ...]] | Err[ParseError]:
        """Every subagent this thread spawned, in spawn order, with its terminal status."""
        read = await self._read()
        if isinstance(read, Err):
            return read
        children: dict[ThreadId, Child] = {}
        for e in read.value.fold.events:
            if isinstance(e, AgentSpawnedEvent):
                children[e.data.child_thread_id] = Child(e.data.child_thread_id, "running")
            elif isinstance(e, AgentFinishedEvent):
                child = e.data.child_thread_id
                children[child] = Child(child, e.data.status)
        return Ok(tuple(children.values()))

    async def usage(self) -> Ok[UsageTotals] | Err[ParseError]:
        """Token totals over this branch (a fork counts its parent's prefix). A response whose
        count the provider didn't report is counted in unknown_responses, never as zero."""
        read = await read_log(self.store, self.branch)
        return read if isinstance(read, Err) else Ok(usage_totals(read.value.fold))

    async def cost(self, *, tree: bool = False) -> Ok[Cost | None] | Err[ParseError]:
        """What the thread spent, in nano-units of its pinned currency (USD for `agent()`), with
        a conservative upper bound; None when the thread pins no prices. `tree=True` adds every
        descendant subagent, in the root's currency (else the first priced descendant's; None
        when none is priced). A thread that spent money it can't add (unpriced, or another
        currency) makes the total incomplete and unbounded; one that made no model request
        changes nothing."""
        read = await read_log(self.store, self.branch)
        if isinstance(read, Err):
            return read
        if not tree:
            return cost(read.value.fold)
        return await tree_cost(self.store, self.id, read.value)

    async def cache_breaks(self) -> Ok[tuple[CacheBreak, ...]] | Err[ParseError]:
        """Turns whose prompt-cache reads dropped sharply, each with its likely cause. A thread
        that pinned no context policy is judged by the default cache ttl."""
        read = await read_log(self.store, self.branch)
        if isinstance(read, Err):
            return read
        fold = read.value.fold
        return Ok(cache_breaks(fold, defaults.context(fold).cache_ttl_ms))

    async def branches(self) -> tuple[BranchInfo, ...]:
        """The thread's visible branches; a forking or failed fork is never listed."""
        rows = await (await open_store(self.store)).tables.branches(self.id)
        return tuple(
            BranchInfo.model_validate(
                {"branch_id": r.branch_id, "mode": "live", "runnable": r.state == "ready"}
                | ({} if r.parent_branch_id is None else {"parent_branch_id": r.parent_branch_id})
                | ({} if r.fork_at_seq is None else {"fork_at_seq": r.fork_at_seq})
            )
            for r in rows
        )

    async def pending_approvals(self) -> Ok[tuple[PendingApproval, ...]] | Err[ParseError]:
        """Open challenges on this branch, with the rules an approver may keep."""
        read = await self._read()
        if isinstance(read, Err):
            return read
        fold = read.value.fold
        member = await member_of(await open_store(self.store), fold)
        return Ok(with_member(approvals.pending(fold), member))

    async def approve(
        self,
        challenge_id: str,
        principal: Principal,
        *,
        remember_rule: PermissionRule | None = None,
    ) -> Controlled:
        """approval_granted, single-use, by an approver."""
        at = (self.id, self.branch)
        return await approvals.decide(
            self.store,
            at,
            challenge_id,
            principal,
            "granted",
            authority=self.authority,
            remember_rule=remember_rule,
        )

    async def deny(
        self, challenge_id: str, principal: Principal, *, reason: str | None = None
    ) -> Controlled:
        """approval_denied, single-use, by an approver."""
        at = (self.id, self.branch)
        return await approvals.decide(
            self.store,
            at,
            challenge_id,
            principal,
            "denied",
            authority=self.authority,
            reason=reason,
        )

    async def answer(
        self, call_id: CallId, answer: str | Sequence[str], principal: Principal
    ) -> Controlled:
        """Answers an open ask_user question."""
        return await control.answer(self.store, self.branch, call_id, answer, principal)

    async def resolve_parked(
        self,
        effect_key: str,
        resolution: Literal["assume_done", "assume_not_done"],
        principal: Principal,
    ) -> Controlled:
        """A human settles a parked effect; assume_not_done accepts duplicate risk."""
        denied = await refused(self.store, self.id, principal, self.authority)
        if denied is not None:
            return denied
        return await control.resolve_parked(
            self.store, self.branch, effect_key, resolution, principal
        )

    async def cancel(self, principal: Principal) -> Controlled:
        """Durable cancel_requested; unsettled effects park. Tree-wide: every unfinished
        descendant subagent is barred too."""
        return await tree.cancel_tree(self.store, self.branch, principal)

    async def set_model(self, settings: SettingsChange, principal: Principal) -> Controlled:
        """settings_changed{reason: user}."""
        return await control.set_model(self.store, self.branch, settings, principal)

    async def set_mode(self, mode: PermissionMode, principal: Principal) -> Controlled:
        """mode_changed."""
        return await control.set_mode(self.store, self.branch, mode, principal)

    async def replay(self) -> Ok[None] | Err[ParseError]:
        """Re-renders every recorded model request of this branch from the log and checks it
        byte for byte: no model or tool calls, no appends. The first failure names its seq.
        Run it over real threads in CI to prove an upgrade still reproduces them."""
        read = await read_log(self.store, self.branch)
        if isinstance(read, Err):
            return read
        sq = await open_store(self.store)
        return await sq.reading(partial(verify_requests, read.value.fold.events))

    async def compact(self, principal: Principal, *, instructions: str | None = None) -> Controlled:
        """Records a compaction request while the thread is idle; its next run summarizes
        everything up to it first. The outcome is in the timeline, not an error here."""
        return await style.compact(self.store, self.branch, principal, instructions)

    async def set_output_style(self, name: str, principal: Principal) -> Controlled:
        """Switches later replies to one of the agent's pinned output styles, while idle."""
        return await style.set_output_style(self.store, self.branch, name, principal)

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
    if isinstance(read, Err) and read.error.code == "branch_not_found":
        return Err(ParseError("not_found", read.error.message))
    if isinstance(read, Err):
        return Err(read_error(read.error))
    if read.value.fold.thread_id != thread_id:
        return Err(ParseError("not_found", f"no branch {branch.value} in thread {thread_id}"))
    return Ok(Thread(thread_id, branch.value, store, sandbox=sandbox))
