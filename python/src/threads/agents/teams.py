"""A team thread's side of a run (spec/schema/README.md, "Teams"): the lead's first append names its
new team; its loop gets the team tools' runtime; and the lead of an in-process run drives its
team's worker for as long as the run is open."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.config import UnboundError
from threads.agents.definition import Definition
from threads.agents.dynamic_agent import member_definition
from threads.agents.pinned import outside_any_branch
from threads.agents.servers import with_servers
from threads.agents.setup import set_up
from threads.agents.store import Store, now_ms
from threads.agents.team_budgets import recipient_of, run_covering
from threads.agents.team_check import agents_of
from threads.agents.team_worker import MemberRun, TeamWorker, WorkerEnv
from threads.agents.workspace import with_workspace
from threads.log import ModelRef, Policy, Principal, ThreadStartedEvent
from threads.log.digest import sha256_hex
from threads.loop.team_runtime import TeamAgentPin, TeamRuntime
from threads.loop.teams import settled
from threads.store import SqliteStore, Writer
from threads.store.lines import uuid7
from threads.team.dynamic import KEPT, Choice, Template
from threads.team.host_open import ensure_host_team
from threads.team.policy import leads, startable
from threads.team.rows import cancel_pending_for


def lead_started[D](
    definition: Definition[D], started: Mapping[str, JsonValue], now: int
) -> dict[str, JsonValue]:
    """A lead's thread_started names a new team, whose log its first append opens. A host rule
    that allows start makes its from a lead too, with no team of its own (lane 29C)."""
    if not leads(definition.team, definition.rules):
        return dict(started)
    team: JsonValue = {"id": uuid7(now), "log_thread_id": uuid7(now), "log_branch_id": uuid7(now)}
    return {**started, "team": team}


async def member_pin[D](definition: Definition[D], *, host: bool = False) -> TeamAgentPin:
    """Its pin as a team member, after setup: config_hash, the canonical config, and what one
    request of it reserves; a template's also what a start may choose. `host` pins it as a host
    member instead, whose team grants nothing (Teams Phase 2). Raises ConfigError, also for a
    dynamic member whose chosen tool its template no longer has."""
    await set_up(definition)
    async with AsyncExitStack() as stack:
        connected = await with_servers(definition, stack, outside_any_branch)
        resolved, _ = await with_workspace(connected)
        member = replace(resolved, in_team=True, host_member=host)
        started, config = member.pin()
        specs = member.spec_artifacts()
        names = [s.name for s in member.specs()]
    if definition.dynamic is not None:
        gone = [t for t in definition.dynamic.define.tools if t not in names]
        if gone:
            raise UnboundError("invalid_config", f"{definition.name} no longer has {gone[0]}")
    template = None
    if definition.models and definition.dynamic is None:
        choosable = tuple(n for n in names if n not in KEPT)
        template = Template(choosable, tuple(k for k, _ in definition.models))
    policy = started.get("policy")
    return TeamAgentPin(
        sha256_hex(config),
        config,
        ModelRef.model_validate(started["model"]),
        _object(started["model_params"]),
        None if policy is None else Policy.model_validate(policy),
        definition.budget,
        template,
        specs,
    )


def _object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise AssertionError("model_params is an object")
    return value


type Pin = Callable[[str, Choice | None], Awaitable[TeamAgentPin | None]]
"""An agent a team lists, pinned as a member (a dynamic one with a start's choice)."""


def pins[D](
    definition: Definition[D],
) -> Pin:
    """The pins of the agents start may name, each made once, on first use; a dynamic member's
    with its start's choice, each time."""
    made: dict[str, TeamAgentPin | None] = {}

    listed = startable(definition.team, definition.rules, definition.rule_agents)

    async def pin(agent: str, choice: Choice | None) -> TeamAgentPin | None:
        found = next((a for a in listed if a.name == agent), None)
        if found is not None and choice is not None:
            return await member_pin(member_definition(found, choice.define, choice.starter))
        if agent not in made:
            made[agent] = None if found is None else await member_pin(found)
        return made[agent]

    return pin


@dataclass(frozen=True, slots=True)
class TeamSide:
    runtime: TeamRuntime
    stop: Callable[[], Awaitable[None]]


async def _nothing() -> None:
    pass


def _quiet() -> None:
    """A caller drives no team worker, so an append of its own wakes nothing here."""


async def host_member_pin(definition: Definition[None]) -> TeamAgentPin:
    """A host member's pin: what its own start, its rebind and Host.team all hash it as."""
    return await member_pin(definition, host=True)


async def open_host_team(sq: SqliteStore, tenant: str, members: Sequence[Definition[None]]) -> None:
    """The tenant's host team, opened before a thread of it addresses a host member. Each
    member's pin is stored before the member_started that names it, as a start's is."""
    configs: dict[str, str] = {}
    for definition in members:
        pinned = await host_member_pin(definition)
        for raw in (pinned.config, *pinned.specs):
            await sq.put_artifact(raw)
        configs[definition.name] = pinned.config_hash
    await ensure_host_team(sq, tenant, configs, holder=f"open-{uuid7(now_ms())}", clock=now_ms)


async def team_of[D](  # noqa: PLR0913, PLR0917 - the run, its store, and how it runs a member
    definition: Definition[D],
    member: MemberRun | None,
    writer: Writer,
    store: Store,
    sq: SqliteStore,
    run: Callable[[Definition[None], MemberRun], Awaitable[None]],
    principal: Principal,
) -> TeamSide | None:
    """A team thread's runtime and what stops it; None for any other thread, a caller aside.
    One run, one authority (design §2.6): its ordinary mail is its own principal's; mail of
    another waits for a run under that one."""
    own = leads(definition.team, definition.rules)
    if not own and member is None and not definition.host_members:
        return None
    pin, limits = pins(definition), definition.team_limits
    thread = writer.fold.thread_id
    if thread is None:
        raise AssertionError("an acquired branch has a thread")

    async def cancel_pending() -> bool:
        return await sq.run(lambda c: cancel_pending_for(c, thread))

    # A thread that may address a host member opens its tenant's host team first: the ids are
    # derived, so a concurrent open is already_open (Teams Phase 2, the lazy open).
    if definition.host_members:
        await open_host_team(sq, principal.tenant, definition.host_members)
    if not own and member is None:
        # A caller: in no team, driving no members, but it may send to and ask a host member.
        runtime = TeamRuntime(
            pin,
            limits,
            settled,
            _quiet,
            principal=principal,
            recipient=recipient_of(sq),
            cancel_pending=cancel_pending,
            rules=definition.rules,
        )
        return TeamSide(runtime, _nothing)
    if member is not None:
        runtime = TeamRuntime(
            pin,
            limits,
            settled,
            member.notify,
            principal=member.principal,
            run_covering=run_covering(sq),
            recipient=recipient_of(sq),
            cancel_pending=cancel_pending,
            abort=member.abort,
            rules=definition.rules,
        )
        return TeamSide(runtime, _nothing)

    def team() -> str | None:
        started = next((e for e in writer.fold.events if isinstance(e, ThreadStartedEvent)), None)
        return None if started is None or started.data.team is MISSING else started.data.team.id

    listed = startable(definition.team, definition.rules, definition.rule_agents)
    env = WorkerEnv(store, sq, team, agents_of(listed), member_pin, run)
    worker = TeamWorker(env)
    worker.start()
    runtime = TeamRuntime(
        pin,
        limits,
        settled,
        worker.notify,
        worker.progress,
        principal=principal,
        busy=worker.busy,
        recipient=recipient_of(sq),
        cancel_pending=cancel_pending,
        rules=definition.rules,
    )
    return TeamSide(runtime, worker.stop)
