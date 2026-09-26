"""The in-process team worker (spec/schema/README.md, "Teams"; decision 17): while a lead's run is
open, it materializes every starting member of the lead's team (and of each nested team), runs
each member whose mail is pending, refuses mail that reaches an ended member, and resumes a
member whose turn was left open with its lease free (hostless recovery). The lead consumes its
own mail in its run. One member branch runs at a time; members run concurrently.

A cancel for a member in flight stops its run at once (design §4.14): the worker owns that run's
task and cancels it, and the member applies the cancel at its next step boundary. A member whose
setup failed for now (an MCP connect, an adapter's setup) is left as it is and tried again with
backoff, never ended. Mirrors TypeScript's agent/team/worker.ts."""

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.definition import Definition
from threads.agents.store import Store, now_ms
from threads.agents.team_budgets import ancestors_of, started_cap
from threads.agents.team_log_mail import take_team_log_mail
from threads.agents.team_rebind import bound, rebind
from threads.agents.team_scan import closed, lease_free, members_under, principal_of
from threads.agents.team_units import end_unbound, take_mail
from threads.log import (
    BranchId,
    MailEnvelope,
    MemberStartedEvent,
    Parent,
    Principal,
    ThreadId,
    ThreadStartedEvent,
)
from threads.log import UserInputEvent as _Input
from threads.loop.covering import Covering
from threads.loop.team_runtime import TeamAgentPin
from threads.result import Err
from threads.store import SqliteStore, lease
from threads.team.batch import Mint
from threads.team.claim import claim_mail
from threads.team.constants import TEAM_CONSTANTS
from threads.team.consume import may_resume
from threads.team.deadline import next_deadline
from threads.team.materialize import (
    MaterializeOptions,
    Rebind,
    materialize,
    started_by,
)
from threads.team.rows import (
    MemberRow,
    cancel_pending_for,
    mail_envelope,
    own_rows,
    pending_for,
    team_row,
)


@dataclass(frozen=True, slots=True)
class MemberRun:
    """One member branch, as the worker runs it."""

    thread: ThreadId
    branch: BranchId
    parent: Parent
    principal: Principal
    """The principal of the member's task: its turns' actor."""
    holder: str
    notify: Callable[[], None]
    covering: tuple[Covering, ...]
    """Every ancestor's budget: it covers the member too."""
    abort: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when the worker stops this run for a cancel: its model call ends at once."""


@dataclass(frozen=True, slots=True)
class WorkerEnv:
    store: Store
    sq: SqliteStore
    team: Callable[[], str | None]
    """The lead's team: None until its first append names it."""
    agents: Mapping[str, Definition[None]]
    """Every agent of the lead's team tree, by name."""
    pin: Callable[[Definition[None]], Awaitable[TeamAgentPin]]
    run: Callable[[Definition[None], MemberRun], Awaitable[None]]
    mint: Mint | None = None
    claim_ttl_ms: int = TEAM_CONSTANTS.claim_ttl_ms
    setup_attempts: int = TEAM_CONSTANTS.setup_attempts
    """How many setup failures in a row end a member setup_failed; tests inject fewer."""


type Unit = Callable[[], Awaitable[bool]]
"""Work on a member: False when it could do nothing (lease held elsewhere, setup failed for now)."""

_FIRST_BACKOFF_MS = 250
"""The first wait before retrying a member that did nothing; it doubles up to the claim TTL."""


class _LaterError(Exception):
    """A member's setup failed for now: materialize stops, and the member is tried again later."""


class TeamWorker:
    def __init__(self, env: WorkerEnv) -> None:
        self._env = env
        self._running: dict[str, asyncio.Task[None]] = {}
        self._aborts: dict[str, asyncio.Event] = {}
        """Each member run's stop: set for a pending cancel, it ends the run's model call."""
        self._applying: set[str] = set()
        """Member runs launched with a cancel pending: each applies it, so none is stopped."""
        self._backoff: dict[str, tuple[int, int]] = {}
        self._setups: dict[str, int] = {}
        """How many times in a row each member's setup has failed for now."""
        """Members whose last unit did nothing: (not before, the wait that set it)."""
        self._changed = asyncio.Event()
        self._failure: BaseException | None = None
        self._stopped = False
        self._loop: asyncio.Task[None] | None = None
        self._token = f"worker-{uuid.uuid4().hex}"
        self._recovered = False
        self._owed = False

    def notify(self) -> None:
        """A team thread appended: look for work now, and wake whoever waits on progress."""
        self._owed = True
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def busy(self) -> bool:
        """Whether a member run is in flight, or may be: until the recovery pass has looked,
        a parked member may be about to run on. A failed member run counts, so a lead parked on
        its members waits on progress, which raises the failure. So does a pass owed since the
        last append: the work it launches may be what the lead waits on."""
        return bool(self._running) or self._owed or not self._recovered or self._failure is not None

    def progress(self) -> Awaitable[None]:
        """Returns on the team's next progress after this call; raises once a member run failed
        with a bug."""
        return self._next(self._changed)

    async def _next(self, changed: asyncio.Event) -> None:
        if self._failure is None:
            await changed.wait()
        if self._failure is not None:
            raise self._failure

    def start(self) -> None:
        self._loop = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        """Stops looking for work and waits for the member runs in flight, then raises a member
        run's bug if there was one. When the lead closed its team those runs are stopped first:
        each applies its cancel on its next step and ends."""
        self._stopped = True
        self.notify()
        if self._loop is not None:
            await self._loop
        team = self._env.team()
        if team is not None and await self._env.sq.run(lambda c: closed(c, team)):
            for thread in list(self._running):
                self._abort(thread)
        await asyncio.gather(*self._running.values(), return_exceptions=True)
        # A member run's bug is never lost, whatever the lead's run was waiting on when it ended.
        if self._failure is not None:
            raise self._failure

    async def _run(self) -> None:
        recovering = True
        # The recovery pass always runs: even a worker stopped at once looks at its members.
        while recovering or not self._stopped:
            changed, self._owed = self._changed, False
            await self._pass(recovering=recovering)
            if recovering:
                recovering, self._recovered = False, True
                self.notify()
            poll = TEAM_CONSTANTS.wake_poll_in_process_ms / 1000
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(changed.wait(), poll)

    async def _pass(self, *, recovering: bool) -> None:
        team = self._env.team()
        if team is None:
            return
        rows = await self._env.sq.run(lambda c: members_under(c, team))
        for each in sorted({team, *(r.team_id for r in rows)}):
            await take_team_log_mail(self._env.sq, each, self._env.mint)
        for row in rows:
            await self._visit(row, recovering=recovering)

    async def _visit(self, row: MemberRow, *, recovering: bool) -> None:
        thread = row.thread_id
        cancelled = await self._env.sq.run(lambda c: cancel_pending_for(c, thread))
        if thread in self._running:
            # A cancel reached a member in flight: stop its run now, unless the run was launched
            # to apply it.
            if cancelled and thread not in self._applying:
                self._abort(thread)
            return
        # A pending cancel is applied at once, backoff or not: it never needs the member's setup.
        if self._backoff.get(thread, (0, 0))[0] > now_ms() and not cancelled:
            return
        work = await self._work(row, recovering=recovering, cancelled=cancelled)
        if work is not None:
            if cancelled:
                self._applying.add(thread)
            self._launch(thread, work)

    def _abort(self, thread: str) -> None:
        stop = self._aborts.get(thread)
        if stop is not None:
            stop.set()

    async def _work(self, row: MemberRow, *, recovering: bool, cancelled: bool) -> Unit | None:
        """What a member needs now, if anything. A pending cancel always wakes it: its run
        applies the cancel (a run stopped for it holds the cancel's claim)."""
        # A closed team's members start or resume only to apply the lead's cancel.
        shut = await self._env.sq.run(lambda c: closed(c, row.team_id))
        if row.state != "ended" and shut and not cancelled:
            return None
        if row.state == "starting":
            return lambda: self._materialize(row)
        branch = row.branch_id
        if branch is None:
            return None
        pending = await self._env.sq.run(lambda c: pending_for(c, own_rows(c, row.thread_id)))
        # A parked member runs again at a run's start (what it waits on may be answered by now),
        # for mail that may resume it, and once an ask or a wait it parked on is due. Waking on a
        # due deadline or on mail to refuse waits for a free lease: its holder does that work, and
        # a launch that can't acquire would relaunch at once.
        if row.state == "parked":
            wake = (
                recovering
                or cancelled
                or await self._claim([m for m in pending if may_resume(m)])
                or (await self._free(branch) and await self._due(BranchId(branch)))
            )
            return (lambda: self._member(row, BranchId(branch))) if wake else None
        if row.state == "ended":
            return (
                (lambda: self._take_mail(BranchId(branch)))
                if pending and await self._free(branch)
                else None
            )
        # A turn left open with its lease free is resumed (hostless recovery).
        stranded = row.state == "running" and (recovering or cancelled or await self._free(branch))
        woken = await self._claim(pending)
        return (lambda: self._member(row, BranchId(branch))) if woken or stranded else None

    async def _take_mail(self, branch: BranchId) -> bool:
        """An ended member's writer refuses the mail that still reaches it. False: its lease is
        held elsewhere."""
        return (await take_mail(self._env.sq, self._env.mint, branch)) is not None

    async def _claim(self, mail: Sequence[MailEnvelope]) -> bool:
        """mail.claim (design §4.6) on the first row this worker would wake the member for: a
        live claim of another worker means that worker wakes it. Correctness never depends on it:
        the lease holder consumes."""
        if not mail:
            return False
        first, now, ttl = mail[0].mail_id, now_ms(), self._env.claim_ttl_ms
        got = await self._env.sq.run(lambda c: claim_mail(c, first, self._token, now, ttl))
        return got == "claimed"

    async def _due(self, branch: BranchId) -> bool:
        """An ask or a wait the parked member waits on is due: its writer closes it.
        ponytail: reads the member's log each pass; keep its next deadline per head if parked
        members grow many."""
        read = await self._env.sq.read(branch, now_ms())
        if isinstance(read, Err):
            return False
        fold = read.value.fold
        due = await self._env.sq.run(lambda c: next_deadline(c, fold, branch))
        return due is not None and due <= now_ms()

    async def _free(self, branch: str) -> bool:
        now = now_ms()
        return await self._env.sq.run(lambda c: lease_free(c, branch, now))

    def _launch(self, thread: str, work: Unit) -> None:
        """Runs one unit for a member. One that did nothing backs off, so a pass never relaunches
        it at once; one that did something (or was stopped for a cancel) wakes the next pass."""

        async def run() -> None:
            try:
                if await work():
                    self._backoff.pop(thread, None)
                    self.notify()
                else:
                    self._later(thread)
            except Exception as error:
                self._failure = self._failure or error
                self.notify()
            finally:
                self._running.pop(thread, None)
                self._aborts.pop(thread, None)
                self._applying.discard(thread)

        self._aborts[thread] = asyncio.Event()
        self._running[thread] = asyncio.get_running_loop().create_task(run())

    def _later(self, thread: str) -> None:
        last = self._backoff.get(thread, (0, 0))[1]
        wait = min(max(last * 2, _FIRST_BACKOFF_MS), TEAM_CONSTANTS.claim_ttl_ms)
        self._backoff[thread] = (now_ms() + wait, wait)

    async def _materialize(self, row: MemberRow) -> bool:
        holder = f"team-{uuid.uuid4().hex}"
        o = MaterializeOptions(self._settled_rebind, holder, lease.TTL_MS, now_ms, self._env.mint)
        try:
            got = await materialize(self._env.sq, row.team_id, row.name, o)
        except _LaterError:
            return False
        if isinstance(got, Err):
            raise AssertionError(f"materialize {row.name}: {got.error.message}")
        self.notify()
        writer = got.value.writer
        if got.value.status == "materialized" and writer is not None:
            return await self._member(row, writer.branch_id, holder)
        return True

    async def _settled_rebind(self, started: MemberStartedEvent, task: MailEnvelope) -> Rebind:
        """materialize's rebind: a setup that failed for now stops it (_LaterError)."""
        got = self._counted(
            started.data.thread_id, await rebind(self._env.agents, self._env.pin, started, task)
        )
        if got == "later":
            raise _LaterError
        return got

    def _counted(self, thread: str, got: Rebind | Literal["later"]) -> Rebind | Literal["later"]:
        """A setup that failed for now, counted: once it has failed Setup attempts times in a row
        the member ends setup_failed; any other outcome resets the count."""
        failed = self._setups.get(thread, 0) + 1 if got == "later" else 0
        self._setups[thread] = failed
        return Rebind("setup_failed") if failed >= self._env.setup_attempts else got

    async def _started(
        self, row: MemberRow, mail_id: str
    ) -> tuple[MemberStartedEvent, MailEnvelope]:
        """A running member's member_started and task, read from its starter's log."""
        task = await self._env.sq.run(lambda c: mail_envelope(c, mail_id))
        team = await self._env.sq.run(lambda c: team_row(c, row.team_id))
        if task is None or team is None:
            raise AssertionError(f"member {row.name} has no task mail")
        started = await started_by(self._env.sq, (team.team_log_branch_id, task), row, now_ms())
        if isinstance(started, Err):
            raise AssertionError(f"member {row.name}: {started.error.message}")
        return started.value, task

    async def _member(self, row: MemberRow, branch: BranchId, holder: str | None = None) -> bool:
        """Runs a member branch until it is idle, parked or ended; one whose definition can't be
        rebound here ends failed instead. False: nothing ran (its setup failed for now, or its
        lease is held elsewhere)."""
        read = await self._env.sq.read(branch, now_ms())
        if isinstance(read, Err):
            raise AssertionError(f"member {row.name}: {read.error.message}")
        events = read.value.fold.events
        started = next((e for e in events if isinstance(e, ThreadStartedEvent)), None)
        task = next((e for e in events if isinstance(e, _Input)), None)
        if started is None or task is None or not isinstance(started.data.parent, Parent):
            raise AssertionError(f"member {row.name} has no task")
        parent, holder = started.data.parent, holder or f"team-{uuid.uuid4().hex}"
        if task.data.mail_id is MISSING:
            raise AssertionError(f"member {row.name}'s first input is not its task")
        member_started, envelope = await self._started(row, task.data.mail_id)
        found = bound(self._env.agents, member_started, envelope)
        rebound = self._counted(
            row.thread_id,
            await rebind(self._env.agents, self._env.pin, member_started, envelope),
        )
        if rebound == "later":
            return False
        if found is None or rebound.status != "ok":
            code = "pin_unavailable" if rebound.status == "ok" else rebound.status
            return await end_unbound(self._env.sq, self._env.mint, branch, holder, code)
        fold = read.value.fold
        principal = await self._env.sq.run(lambda c: principal_of(c, fold, row))
        run = MemberRun(
            ThreadId(row.thread_id),
            branch,
            parent,
            principal or task.actor.principal,
            holder,
            self.notify,
            (
                *await started_cap(self._env.sq, parent),
                *await ancestors_of(self._env.sq, parent),
            ),
            self._aborts.get(row.thread_id, asyncio.Event()),
        )
        await self._env.run(found, run)
        return True
