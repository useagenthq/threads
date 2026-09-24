"""What a team lead's run returns (spec/api.json TeamRunResult, Team, TeamRef): every RunResult
variant, also carrying the lead's team."""

from dataclasses import dataclass, field
from typing import assert_never

from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.results import (
    BudgetExhausted,
    Cancelled,
    Completed,
    Failed,
    HandedOff,
    Parked,
    RunResult,
)
from threads.agents.store import now_ms, open_store
from threads.log import ThreadStartedEvent
from threads.result import Err


@dataclass(frozen=True, slots=True)
class TeamRef:
    """Which team: team.ref, for openTeam in another process."""

    tenant: str
    id: str


@dataclass(frozen=True, slots=True)
class Team:
    """A team's handle. Lane 21F adds its operator methods (start, send, ask, wait, cancel,
    members, events, ask_status); until then it names the team."""

    ref: TeamRef


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


async def with_team[O](result: RunResult[O]) -> TeamRunResult[O]:
    """The result with the lead's team: the team its thread_started names, in its store's
    tenant."""
    thread = result.thread
    sq = await open_store(thread.store)
    read = await sq.read(thread.branch, now_ms())
    events = () if isinstance(read, Err) else read.value.fold.events
    started = next((e for e in events if isinstance(e, ThreadStartedEvent)), None)
    if started is None or started.data.team is MISSING:
        raise AssertionError(f"thread {thread.id} leads no team")
    team = Team(TeamRef(thread.store.tenant, started.data.team.id))
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
