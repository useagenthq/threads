"""A team thread's side of a run (spec/schema/README.md, "Teams"): the lead's first append names its
new team; its loop gets the team tools' runtime; and the lead of an in-process run drives its
team's worker for as long as the run is open."""

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.definition import Definition
from threads.agents.pinned import outside_any_branch
from threads.agents.servers import with_servers
from threads.agents.setup import set_up
from threads.agents.store import Store
from threads.agents.team_budgets import run_covering
from threads.agents.team_check import agents_of
from threads.agents.team_worker import MemberRun, TeamWorker, WorkerEnv
from threads.log import ModelRef, Policy, ThreadStartedEvent
from threads.log.digest import sha256_hex
from threads.loop.team_runtime import TeamAgentPin, TeamRuntime
from threads.loop.teams import settled
from threads.store import SqliteStore, Writer
from threads.store.lines import uuid7


def lead_started[D](
    definition: Definition[D], started: Mapping[str, JsonValue], now: int
) -> dict[str, JsonValue]:
    """A lead's thread_started names a new team, whose log its first append opens."""
    if definition.team is None:
        return dict(started)
    team: JsonValue = {"id": uuid7(now), "log_thread_id": uuid7(now), "log_branch_id": uuid7(now)}
    return {**started, "team": team}


async def member_pin[D](definition: Definition[D]) -> TeamAgentPin:
    """Its pin as a team member, after setup: config_hash, the canonical config, and what one
    request of it reserves. Raises ConfigError."""
    await set_up(definition)
    async with AsyncExitStack() as stack:
        connected = await with_servers(definition, stack, outside_any_branch)
        started, config = replace(connected, in_team=True).pin()
    policy = started.get("policy")
    return TeamAgentPin(
        sha256_hex(config),
        config,
        ModelRef.model_validate(started["model"]),
        _object(started["model_params"]),
        None if policy is None else Policy.model_validate(policy),
        definition.budget,
    )


def _object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise AssertionError("model_params is an object")
    return value


def _pins[D](definition: Definition[D]) -> Callable[[str], Awaitable[TeamAgentPin | None]]:
    """The pins of the agents start may name, each made once, on first use."""
    made: dict[str, TeamAgentPin | None] = {}

    async def pin(agent: str) -> TeamAgentPin | None:
        if agent not in made:
            found = next((a for a in definition.team or () if a.name == agent), None)
            made[agent] = None if found is None else await member_pin(found)
        return made[agent]

    return pin


@dataclass(frozen=True, slots=True)
class TeamSide:
    runtime: TeamRuntime
    stop: Callable[[], Awaitable[None]]


async def _nothing() -> None:
    pass


def team_of[D](  # noqa: PLR0913, PLR0917 - the run, its store, and how it runs a member
    definition: Definition[D],
    member: MemberRun | None,
    writer: Writer,
    store: Store,
    sq: SqliteStore,
    run: Callable[[Definition[None], MemberRun], Awaitable[None]],
) -> TeamSide | None:
    """A team thread's runtime and what stops it; None for any other thread."""
    if definition.team is None and member is None:
        return None
    pin, limits = _pins(definition), definition.team_limits
    if member is not None:
        runtime = TeamRuntime(
            pin,
            limits,
            settled,
            member.notify,
            principal=member.principal,
            run_covering=run_covering(sq),
        )
        return TeamSide(runtime, _nothing)
    if definition.team is None:
        return None

    def team() -> str | None:
        started = next((e for e in writer.fold.events if isinstance(e, ThreadStartedEvent)), None)
        return None if started is None or started.data.team is MISSING else started.data.team.id

    env = WorkerEnv(store, sq, team, agents_of(definition.team), member_pin, run)
    worker = TeamWorker(env)
    worker.start()
    runtime = TeamRuntime(pin, limits, settled, worker.notify, worker.progress, busy=worker.busy)
    return TeamSide(runtime, worker.stop)
