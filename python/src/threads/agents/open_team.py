"""open_team (spec/api.json openTeam): a handle on a team another process ran, or to act as another
principal. RunResult.team is the common path."""

from dataclasses import dataclass
from typing import Literal

from threads.agents.store import Store, open_store, scoped
from threads.agents.team_handle import HandleEnv, Team
from threads.agents.team_handle_types import TeamRef
from threads.agents.team_leads import lead_named
from threads.log import Principal
from threads.result import Err, Ok
from threads.team.rows import member_rows, team_row


@dataclass(frozen=True, slots=True)
class OpenTeamError:
    code: Literal["not_found", "forbidden"]
    message: str


async def open_team(
    store: Store, ref: TeamRef, *, principal: Principal
) -> Ok[Team] | Err[OpenTeamError]:
    """A Team handle acting as `principal`: another tenant's is forbidden, and a team not stored
    in `ref.tenant` is not_found."""
    if principal.tenant != ref.tenant:
        message = f"the principal is of tenant {principal.tenant}, not the team's"
        return Err(OpenTeamError("forbidden", message))
    sq = await open_store(scoped(store, ref.tenant))
    team = await sq.run(lambda c: team_row(c, ref.id))
    if team is None or team.tenant_id != ref.tenant:
        return Err(OpenTeamError("not_found", f"no team {ref.id} in {ref.tenant}"))
    # The team's agents are its lead's, as this process defines the lead of that name.
    rows = await sq.run(lambda c: member_rows(c, ref.id))
    lead = next((r for r in rows if r.role == "lead"), None)
    defined = None if lead is None else lead_named(lead.agent)
    if defined is None:
        return Ok(Team(HandleEnv(sq, ref, principal, None)))
    return Ok(Team(HandleEnv(sq, ref, principal, defined.pin, defined.limits)))
