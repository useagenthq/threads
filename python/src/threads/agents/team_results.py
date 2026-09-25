"""What a team lead's run returns (spec/api.json TeamRunResult): every RunResult variant, also
carrying the lead's team."""

from dataclasses import dataclass, field
from typing import assert_never

from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.definition import Definition
from threads.agents.results import (
    BudgetExhausted,
    Cancelled,
    Completed,
    Failed,
    HandedOff,
    Parked,
    RunResult,
)
from threads.agents.run import member_runner
from threads.agents.store import now_ms, open_store
from threads.agents.team_check import agents_of
from threads.agents.team_handle import HandleEnv, Team
from threads.agents.team_handle_types import TeamRef
from threads.agents.team_worker import WorkerEnv
from threads.agents.teams import member_pin, pins
from threads.log import ThreadStartedEvent, UserInputEvent
from threads.result import Err


@dataclass(frozen=True, slots=True)
class TeamCompleted[O](Completed[O]):
    team: Team = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class TeamParked(Parked):
    team: Team = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class TeamCancelled(Cancelled):
    team: Team = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class TeamFailed(Failed):
    team: Team = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class TeamBudgetExhausted(BudgetExhausted):
    team: Team = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class TeamHandedOff(HandedOff):
    team: Team = field(kw_only=True)


type TeamRunResult[O] = (
    TeamCompleted[O] | TeamParked | TeamCancelled | TeamFailed | TeamBudgetExhausted | TeamHandedOff
)
"""What TeamAgent.run returns: a RunResult whose every variant also carries team."""


async def with_team[D, O](lead: Definition[D], result: RunResult[O]) -> TeamRunResult[O]:
    """The result with the lead's team: the team its thread_started names, in its store's
    tenant, acting as the principal of the run's request (its latest user_input)."""
    thread = result.thread
    sq = await open_store(thread.store)
    read = await sq.read(thread.branch, now_ms())
    events = () if isinstance(read, Err) else read.value.fold.events
    started = next((e for e in events if isinstance(e, ThreadStartedEvent)), None)
    request = next((e for e in reversed(events) if isinstance(e, UserInputEvent)), None)
    if started is None or started.data.team is MISSING or request is None:
        raise AssertionError(f"thread {thread.id} leads no team")
    ref = TeamRef(thread.store.tenant, started.data.team.id)
    team_id = ref.id
    agents = agents_of(lead.team or ())
    run = member_runner(thread.store)
    worker = WorkerEnv(thread.store, sq, lambda: team_id, agents, member_pin, run)
    env = HandleEnv(sq, ref, request.actor.principal, pins(lead), lead.team_limits, worker)
    team = Team(env)
    match result:
        case Completed():
            return TeamCompleted(result.output, result.thread, team=team)
        case Parked():
            return TeamParked(result.reason, result.pending, result.thread, team=team)
        case Cancelled():
            return TeamCancelled(result.thread, team=team)
        case Failed():
            return TeamFailed(result.error, result.thread, team=team)
        case BudgetExhausted():
            return TeamBudgetExhausted(result.budget, result.thread, team=team)
        case HandedOff():
            return TeamHandedOff(result.thread, result.to_thread, team=team)
        case _:
            assert_never(result)
