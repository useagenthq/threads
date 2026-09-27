"""Running a host member (spec/schema/README.md, "Teams Phase 2"): the team worker's path for a
member with no task and no parent.

A host member is a root thread of its tenant, rebound by its agent name from this host's own
registry, under no ancestor's budget: only its own thread budget, which is a lifetime cap, and
the run budget of whichever caller's mail opened the turn.
"""

import asyncio
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from threads.agents.store import now_ms
from threads.agents.team_rebind import bound, rebind
from threads.agents.team_scan import principal_of
from threads.agents.team_units import end_unbound
from threads.agents.team_worker_env import MemberRun, WorkerEnv
from threads.log import BranchId, MemberStartedEvent, ThreadId
from threads.result import Err
from threads.team.materialize_types import Rebind
from threads.team.rows import MemberRow, team_row

type Counted = Callable[[str, Rebind | Literal["later"]], Rebind | Literal["later"]]
"""The worker's count of setups that failed for now: too many in a row end the member."""


@dataclass(frozen=True, slots=True)
class HostMembers:
    """The worker's host-member path, with what it shares with the worker."""

    env: WorkerEnv
    counted: Counted
    notify: Callable[[], None]
    aborts: Mapping[str, asyncio.Event]

    async def run(self, row: MemberRow, branch: BranchId, holder: str | None) -> bool:
        """Runs the host member until it is idle, parked or ended. False: nothing ran (its
        setup failed for now, or no mail is pending under any principal)."""
        started = await self.started(row)
        rebound = self.counted(
            row.thread_id, await rebind(self.env.agents, self.env.pin, started, None)
        )
        if rebound == "later":
            return False
        found = bound(self.env.agents, started, None)
        holder = holder or f"team-{uuid.uuid4().hex}"
        if found is None or rebound.status != "ok":
            code = "pin_unavailable" if rebound.status == "ok" else rebound.status
            return await end_unbound(self.env.sq, self.env.mint, branch, holder, code)
        read = await self.env.sq.read(branch, now_ms())
        if isinstance(read, Err):
            raise AssertionError(f"host member {row.name}: {read.error.message}")
        fold = read.value.fold
        principal = await self.env.sq.run(lambda c: principal_of(c, fold, row))
        if principal is None:
            return False  # nothing pending under any principal: nothing for this run to take
        run = MemberRun(
            ThreadId(row.thread_id),
            branch,
            None,
            principal,
            holder,
            self.notify,
            (),
            self.aborts.get(row.thread_id, asyncio.Event()),
            host_member=True,
        )
        await self.env.run(found, run)
        return True

    async def started(self, row: MemberRow) -> MemberStartedEvent:
        """A host member's member_started, read from its host team log."""
        team = await self.env.sq.run(lambda c: team_row(c, row.team_id))
        if team is None:
            raise AssertionError(f"no teams row {row.team_id}")
        log = await self.env.sq.read(BranchId(team.team_log_branch_id), now_ms())
        if isinstance(log, Err):
            raise AssertionError(f"host team {row.team_id}: {log.error.message}")
        found = next(
            (
                e
                for e in log.value.fold.events
                if isinstance(e, MemberStartedEvent)
                and e.data.member.name == row.name
                and e.data.member.generation == row.generation
            ),
            None,
        )
        if found is None:
            raise AssertionError(f"no member_started for host member {row.name}")
        return found
