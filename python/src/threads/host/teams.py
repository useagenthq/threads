"""The host as a team worker (design §7 Phase 2, A): each pass drives lane 21's worker for every
open team of the store, so members materialize, take their mail, close their deadlines, apply
their cancels and resume a turn a crash left open, with no lead's run in this process. One worker
per team per host, and one consumer per branch; across processes the lease decides and claims
only deduplicate wakes.

A lead is a member too (§2.4), but no worker runs its branch: mail to an idle hosted lead is
consumed here, which opens its turn, and the host then runs that turn on. It continues the
request the mail belongs to and starts no new run (§2.7.2). Mirrors TypeScript's host/src/
teams.ts.
"""

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from threads.agents.store import now_ms, open_store
from threads.agents.team_hosted import HostedTeam, hosted_teams, team_worker_for
from threads.agents.team_scan import lease_free
from threads.agents.team_units import take_mail
from threads.host.runs import Runner
from threads.log import BranchId, ThreadId
from threads.result import Ok
from threads.store import LOCAL_TENANT, SqliteStore, StoreError
from threads.team.claim import claim_mail
from threads.team.constants import TEAM_CONSTANTS
from threads.team.rows import pending_here

if TYPE_CHECKING:
    from threads.agents.team_worker import TeamWorker

TEAMS_S = 1.0
"""How often a host looks for team work across processes (lane 21's constant). The worker each
team gets polls its own branches every 250 ms."""

_log = logging.getLogger(__name__)


class Teams:
    """Every team this host drives, until each closes or the host stops."""

    def __init__(self, runner: Runner) -> None:
        self._runner = runner
        self._workers: dict[str, TeamWorker] = {}
        self._token = f"host-{uuid.uuid4().hex}"
        """This host's claim token: a row it claimed is one it will wake the lead for."""
        self._stopped = False

    async def run(self) -> None:
        """One pass a second until the host stops."""
        while True:
            await asyncio.sleep(TEAMS_S)
            try:
                await self.once()
            except StoreError as error:
                _log.warning("threads host: teams not driven (%s)", error)

    async def once(self) -> None:
        """A worker for every open team, and the mail an idle lead has waiting."""
        if self._stopped:
            return
        sq = await open_store(self._runner.store(LOCAL_TENANT))
        teams = await sq.run(hosted_teams)
        live = {t.team_id for t in teams}
        for team in [t for t in self._workers if t not in live]:
            await self._drop(team)
        for team in teams:
            await self._ensure(team)
            await self._wake(team)

    async def stop(self) -> None:
        """Stops claiming and waits for the work in flight, leaving every row durable for the
        next host. A member run's bug is reported, never raised: stopping a host is not its
        caller's error."""
        self._stopped = True
        for team in list(self._workers):
            await self._drop(team)

    async def _drop(self, team: str) -> None:
        worker = self._workers.pop(team, None)
        if worker is None:
            return
        try:
            await worker.stop()
        except Exception:
            _log.exception("threads host: the team worker of %s failed", team)

    async def _ensure(self, team: HostedTeam) -> None:
        """A worker for the team while no run of this process is on its lead's branch: that run
        drives the team itself, and one consumer per branch per process is the rule (§4.6)."""
        if team.lead_branch_id is None:
            return
        if self._runner.running(BranchId(team.lead_branch_id)):
            await self._drop(team.team_id)
            return
        if team.team_id in self._workers:
            return
        store = self._runner.store(team.tenant_id)
        bound = await self._runner.bound(store, ThreadId(team.lead_thread_id))
        # No host agent owns that lead: another host drives the team, and this one leaves it be.
        if bound is None:
            return
        worker = team_worker_for(store, await open_store(store), team, bound.definition)
        self._workers[team.team_id] = worker
        worker.start()

    async def _wake(self, team: HostedTeam) -> None:
        """A lead with a free lease: its pending mail consumed under its own writer, then the turn
        that consume opened, or one a crash left open, is run on. A lease held elsewhere (a run of
        this host included) leaves both to its holder, which consumes at its next step boundary."""
        branch = team.lead_branch_id
        if branch is None or self._runner.running(BranchId(branch)):
            return
        store = self._runner.store(team.tenant_id)
        sq = await open_store(store)
        now = now_ms()
        if not await sq.run(lambda c: lease_free(c, branch, now)):
            return
        taken = await self._take(sq, team, BranchId(branch))
        read = await sq.read(BranchId(branch), now_ms())
        if taken or (isinstance(read, Ok) and read.value.fold.in_turn):
            await self._runner.resume(store, ThreadId(team.lead_thread_id), BranchId(branch))

    async def _take(self, sq: SqliteStore, team: HostedTeam, branch: BranchId) -> bool:
        """The lead's pending mail, claimed first so two hosts don't both wake it."""
        thread, now = team.lead_thread_id, now_ms()
        pending = await sq.run(lambda c: pending_here(c, thread, branch))
        if not pending:
            return False
        first, ttl = pending[0].mail_id, TEAM_CONSTANTS.claim_ttl_ms
        claim = await sq.run(lambda c: claim_mail(c, first, self._token, now, ttl))
        if claim != "claimed":
            return False
        taken = await take_mail(sq, None, branch)
        return taken is not None and taken.status != "nothing_pending"
