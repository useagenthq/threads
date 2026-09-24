"""A team thread's side of one run (spec/schema/README.md, "Teams"): its team runtime, stopped
before the lease is handed back; its turns after its input's; and what covers a member."""

from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack
from functools import partial

from threads.agents.framework import Agents
from threads.agents.launch import Launch
from threads.agents.team_worker import MemberRun
from threads.agents.teams import TeamSide
from threads.loop.covering import Covering
from threads.loop.drive import drive, parked
from threads.loop.runtime import Halt, Idle, Runtime
from threads.loop.team_runtime import TeamRuntime
from threads.loop.teams import on_team, team_turns
from threads.reduce.fold import loop_parked
from threads.store import StoredEvent


def team_side(servers: AsyncExitStack, side: TeamSide | None) -> TeamSide | None:
    """A team thread's side of the run, stopped before the lease is handed back: its member runs
    finish first."""
    if side is not None:
        servers.push_async_callback(side.stop)
    return side


async def finish[D](rt: Runtime, agents: Agents[D], halt: Halt, side: TeamSide | None) -> Halt:
    """The run's background children, then a team thread's turns (threads.loop.teams)."""
    halt = await agents.finish(rt, halt)
    return halt if side is None else await team_turns(rt, halt, partial(_again, rt, agents))


async def unparked[D](rt: Runtime, agents: Agents[D], side: TeamSide | None) -> Halt | None:
    """A lead parked on its members first waits for them, as a parent runs the children it is
    parked on: their settlements resume it, and only then does a new input start a turn. None:
    it ended idle, or waits on nothing of its team."""
    held = loop_parked(rt.fold)
    if side is None or not held or not on_team(rt.fold):
        return None
    halt = await team_turns(rt, parked(rt.events, held), partial(_again, rt, agents))
    return None if isinstance(halt, Idle) else halt


async def _again[D](rt: Runtime, agents: Agents[D]) -> Halt:
    """One more turn of a team thread, then its background children as after any turn."""
    return await agents.finish(rt, await drive(rt))


def notifying(
    observe: Callable[[Sequence[StoredEvent]], None], team: TeamRuntime
) -> Callable[[Sequence[StoredEvent]], None]:
    """Every committed, non-empty batch of a team thread also wakes its team's worker."""

    def both(events: Sequence[StoredEvent]) -> None:
        observe(events)
        if events:
            team.notify()

    return both


def covered(launch: Launch | None, member: MemberRun | None) -> tuple[Covering, ...]:
    """Budgets covering this thread as an ancestor's: a launched thread's, or a member's."""
    if member is not None:
        return member.covering
    return () if launch is None else launch.budgets
