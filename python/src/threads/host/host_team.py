"""The host's own team (spec/api.json `host.members`, `Host.team`): the leadless per-tenant team
that holds the tenant's host members.

`Host.team` never rebinds a lead, because a host team has none: it binds the members from the
host's own agent registry, so 29A's config-hash lead rebind is never on the path.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from threads.agents.definition import Definition
from threads.agents.open_team import OpenTeamError
from threads.agents.run import member_runner
from threads.agents.store import Store, open_store
from threads.agents.team_handle import Team
from threads.agents.team_handle_types import TeamRef
from threads.agents.team_operator import HandleEnv
from threads.agents.team_worker import WorkerEnv
from threads.agents.teams import host_member_pin, open_host_team
from threads.log import Principal
from threads.loop.team_runtime import TeamAgentPin
from threads.result import Err, Ok
from threads.team.dynamic import Choice
from threads.team.host_team import host_team_ids
from threads.team.ops import TeamLimits
from threads.team.rows import team_row

type Pin = Callable[[str, Choice | None], Awaitable[TeamAgentPin | None]]


def host_pins(agents: Mapping[str, Definition[None]]) -> Pin:
    """The pin of each host member, made once on first use. A host member is never dynamic:
    only a lead's team lists templates, and a host member leads none."""
    made: dict[str, TeamAgentPin | None] = {}

    async def pin(agent: str, _choice: Choice | None) -> TeamAgentPin | None:
        if agent not in made:
            found = agents.get(agent)
            made[agent] = None if found is None else await host_member_pin(found)
        return made[agent]

    return pin


@dataclass(frozen=True, slots=True)
class HostTeam:
    """A host's members as one team: the agents, and the store they run in."""

    agents: Mapping[str, Definition[None]]

    async def open(self, store: Store, tenant: str) -> None:
        """The tenant's host team, opened if it is not there yet (lazy and idempotent)."""
        if self.agents:
            await open_host_team(await open_store(store), tenant, list(self.agents.values()))

    async def handle(
        self, store: Store, principal: Principal, limits: TeamLimits
    ) -> Ok[Team] | Err[OpenTeamError]:
        """`Host.team`: the handle on the principal's tenant's host team, binding its members
        from the host's own registry. not_found with no members option, or before its open."""
        missing: Literal["not_found"] = "not_found"
        if not self.agents:
            return Err(OpenTeamError(missing, "this host has no members option"))
        sq = await open_store(store)
        ids = host_team_ids(principal.tenant)
        row = await sq.run(lambda c: team_row(c, ids.team))
        if row is None:
            return Err(OpenTeamError(missing, f"no host team in tenant {principal.tenant}"))
        ref = TeamRef(principal.tenant, ids.team)
        worker = WorkerEnv(
            store, sq, lambda: ids.team, dict(self.agents), host_member_pin, member_runner(store)
        )
        return Ok(Team(HandleEnv(sq, ref, principal, host_pins(self.agents), limits, worker)))
