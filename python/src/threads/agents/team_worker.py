"""The in-process team worker (spec/schema/README.md, "Teams"; decision 17): while a lead's run is
open, it materializes every starting member of the lead's team (and of each nested team), runs
each member whose mail is pending, refuses mail that reaches an ended member, and resumes a
member whose turn was left open with its lease free (hostless recovery). The lead consumes its
own mail in its run. One member branch runs at a time; members run concurrently."""

import asyncio
import contextlib
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from threads.agents.config import ConfigError
from threads.agents.definition import Definition
from threads.agents.store import Store, now_ms
from threads.agents.team_budgets import ancestors_of
from threads.log import (
    BranchId,
    MailEnvelope,
    Parent,
    Principal,
    Provenance,
    ThreadId,
    ThreadStartedEvent,
)
from threads.log import UserInputEvent as _Input
from threads.loop.covering import Covering
from threads.loop.team_runtime import TeamAgentPin
from threads.reduce import Fold
from threads.result import Err, Ok
from threads.store import Draft, SqliteStore, lease
from threads.store.lines import uuid7
from threads.store.writer import DecideTx, Refusal
from threads.team.batch import Batch, Mint
from threads.team.claim import claim_mail
from threads.team.constants import TEAM_CONSTANTS
from threads.team.consume import ConsumeContext, consumable, consume
from threads.team.materialize import MaterializeOptions, Rebind, RebindCode, materialize
from threads.team.provenance import turn_provenance
from threads.team.rebind import rebind_failed
from threads.team.rows import MemberRow, member_rows, own_rows, pending_for, team_row
from threads.team.settle import AppendContext


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


class TeamWorker:
    def __init__(self, env: WorkerEnv) -> None:
        self._env = env
        self._running: dict[str, asyncio.Task[None]] = {}
        self._changed = asyncio.Event()
        self._failure: BaseException | None = None
        self._stopped = False
        self._loop: asyncio.Task[None] | None = None
        self._token = f"worker-{uuid.uuid4().hex}"
        self._recovered = False

    def notify(self) -> None:
        """A team thread appended: look for work now, and wake whoever waits on progress."""
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def busy(self) -> bool:
        """Whether a member run is in flight, or may be: until the recovery pass has looked,
        a parked member may be about to run on."""
        return bool(self._running) or not self._recovered

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
        """Stops looking for work and waits for the member runs in flight, unless the lead closed
        its team: those members are being cancelled, and the run returns without them."""
        self._stopped = True
        self.notify()
        if self._loop is not None:
            await self._loop
        team = self._env.team()
        if team is None or not await self._env.sq.run(lambda c: _closed(c, team)):
            await asyncio.gather(*self._running.values(), return_exceptions=True)

    async def _run(self) -> None:
        recovering = True
        # The recovery pass always runs: even a worker stopped at once looks at its members.
        while recovering or not self._stopped:
            changed = self._changed
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
        rows = await self._env.sq.run(lambda c: _members(c, team))
        for row in rows:
            if row.thread_id in self._running:
                continue
            work = await self._work(row, recovering=recovering)
            if work is not None:
                self._launch(row.thread_id, work)

    async def _work(
        self, row: MemberRow, *, recovering: bool
    ) -> Callable[[], Awaitable[None]] | None:
        """What a member needs now, if anything."""
        # A closed team's members are being cancelled: none starts or resumes.
        if row.state != "ended" and await self._env.sq.run(lambda c: _closed(c, row.team_id)):
            return None
        if row.state == "starting":
            return lambda: self._materialize(row)
        branch = row.branch_id
        if branch is None:
            return None
        # A parked member runs again at a run's start: what it waits on may be answered by now.
        if row.state == "parked":
            return (lambda: self._member(row, BranchId(branch))) if recovering else None
        pending = await self._env.sq.run(lambda c: pending_for(c, own_rows(c, row.thread_id)))
        if row.state == "ended":
            return (lambda: self._refuse(BranchId(branch))) if pending else None
        # A turn left open with its lease free is resumed (hostless recovery).
        stranded = row.state == "running" and (recovering or await self._free(branch))
        woken = await self._claim([m for m in pending if consumable(m)])
        return (lambda: self._member(row, BranchId(branch))) if woken or stranded else None

    async def _claim(self, mail: Sequence[MailEnvelope]) -> bool:
        """mail.claim (design §4.6) on the first row this worker would wake the member for: a
        live claim of another worker means that worker wakes it. Correctness never depends on it:
        the lease holder consumes."""
        if not mail:
            return False
        first, now, ttl = mail[0].mail_id, now_ms(), self._env.claim_ttl_ms
        got = await self._env.sq.run(lambda c: claim_mail(c, first, self._token, now, ttl))
        return got == "claimed"

    async def _free(self, branch: str) -> bool:
        def expired(conn: sqlite3.Connection) -> bool:
            row: tuple[int] | None = conn.execute(
                "SELECT expires_at FROM leases WHERE branch_id = ?", (branch,)
            ).fetchone()
            return row is None or row[0] <= now_ms()

        return await self._env.sq.run(expired)

    def _launch(self, thread: str, work: Callable[[], Awaitable[None]]) -> None:
        async def run() -> None:
            try:
                await work()
            except Exception as error:
                self._failure = self._failure or error
            finally:
                self._running.pop(thread, None)
                self.notify()

        self._running[thread] = asyncio.get_running_loop().create_task(run())

    async def _materialize(self, row: MemberRow) -> None:
        holder = f"team-{uuid.uuid4().hex}"
        o = MaterializeOptions(self._rebind, holder, lease.TTL_MS, now_ms, self._env.mint)
        got = await materialize(self._env.sq, row.team_id, row.name, o)
        if isinstance(got, Err):
            raise AssertionError(f"materialize {row.name}: {got.error.message}")
        self.notify()
        writer = got.value.writer
        if got.value.status == "materialized" and writer is not None:
            await self._member(row, writer.branch_id, holder)

    async def _rebind(self, agent: str, config_hash: str) -> Rebind:
        """Rebinds a member's definition by name in this process (design §4.10, prework)."""
        found = self._env.agents.get(agent)
        if found is None:
            return Rebind("pin_unavailable")
        try:
            pinned = await self._env.pin(found)
        except ConfigError:
            return Rebind("pin_unavailable")
        if pinned.config_hash != config_hash:
            return Rebind("pin_mismatch")
        if found.team is None:
            return Rebind("ok")
        now = now_ms()
        team = {"id": uuid7(now), "log_thread_id": uuid7(now), "log_branch_id": uuid7(now)}
        return Rebind("ok", team)

    async def _member(self, row: MemberRow, branch: BranchId, holder: str | None = None) -> None:
        """Runs a member branch until it is idle, parked or ended; one whose definition can't be
        rebound here ends failed instead."""
        read = await self._env.sq.read(branch, now_ms())
        if isinstance(read, Err):
            raise AssertionError(f"member {row.name}: {read.error.message}")
        events = read.value.fold.events
        started = next((e for e in events if isinstance(e, ThreadStartedEvent)), None)
        task = next((e for e in events if isinstance(e, _Input)), None)
        if started is None or task is None or not isinstance(started.data.parent, Parent):
            raise AssertionError(f"member {row.name} has no task")
        parent, holder = started.data.parent, holder or f"team-{uuid.uuid4().hex}"
        found = self._env.agents.get(row.agent)
        rebind = await self._rebind(row.agent, started.data.config_hash)
        if found is None or rebind.status != "ok":
            code = "pin_unavailable" if rebind.status == "ok" else rebind.status
            await self._unbound(branch, holder, code)
            return
        fold = read.value.fold
        principal = await self._env.sq.run(lambda c: principal_of(c, fold, row))
        run = MemberRun(
            ThreadId(row.thread_id),
            branch,
            parent,
            principal or task.actor.principal,
            holder,
            self.notify,
            await ancestors_of(self._env.sq, parent),
        )
        await self._env.run(found, run)

    async def _unbound(self, branch: BranchId, holder: str, code: RebindCode) -> None:
        """A member whose definition can't be rebound here ends failed, under its own writer."""
        got = await self._env.sq.acquire(branch, holder, now_ms)
        # Held elsewhere: its holder runs it.
        if isinstance(got, Err):
            return
        w = got.value
        thread = w.fold.thread_id
        if thread is None:
            raise AssertionError("an acquired branch has a thread")

        def decide(tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
            batch = Batch(tx.fold.seq, tx.now, self._env.mint)
            rebind_failed(AppendContext(tx.conn, batch, thread, branch), tx.fold, code, tx.now)
            return batch.drafts

        ended = await w.append_decided(decide)
        await w.release()
        if not isinstance(ended, Ok):
            raise AssertionError(f"member end: {ended}")

    async def _refuse(self, branch: BranchId) -> None:
        """An ended member's writer refuses the mail that still reaches it."""
        got = await self._env.sq.acquire(branch, f"team-{uuid.uuid4().hex}", now_ms)
        if isinstance(got, Err):
            return
        w = got.value
        thread = w.fold.thread_id
        if thread is None:
            raise AssertionError("an acquired branch has a thread")

        def decide(tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
            batch = Batch(tx.fold.seq, tx.now, self._env.mint)
            consume(ConsumeContext(tx.conn, batch, thread, branch, tx.fold))
            return batch.drafts

        await w.append_decided(decide)
        await w.release()


def principal_of(conn: sqlite3.Connection, fold: Fold, row: MemberRow) -> Principal | None:
    """The one principal a member run acts under (design §2.6: one turn, one authority): its open
    turn's, else that of the first mail it would take. Mail of another principal waits for the
    next run."""
    if fold.in_turn:
        opened = turn_provenance(conn, fold.events)
        return None if opened is None else Provenance.model_validate(opened).principal
    first = next(
        (m for m in pending_for(conn, own_rows(conn, row.thread_id)) if consumable(m)), None
    )
    return None if first is None else first.provenance.principal


def _closed(conn: sqlite3.Connection, team: str) -> bool:
    found = team_row(conn, team)
    return found is not None and found.closed_at is not None


def _members(conn: sqlite3.Connection, root: str) -> list[MemberRow]:
    """The member rows of the team and of every team led by one of its members, recursively."""
    teams, out = [root], list[MemberRow]()
    while teams:
        rows = [r for r in member_rows(conn, teams.pop()) if r.role == "member"]
        out += rows
        for r in rows:
            led: list[tuple[str]] = conn.execute(
                "SELECT team_id FROM teams WHERE lead_thread_id = ?", (r.thread_id,)
            ).fetchall()
            teams += [t for (t,) in led]
    return out
