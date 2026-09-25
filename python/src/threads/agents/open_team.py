"""open_team (spec/api.json openTeam): a handle on a team another process ran, or to act as another
principal. RunResult.team is the common path."""

from dataclasses import dataclass
from typing import Literal

from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.config import ConfigError
from threads.agents.store import Store, now_ms, open_store, scoped
from threads.agents.team_handle import HandleEnv, Team
from threads.agents.team_handle_types import TeamRef
from threads.agents.team_leads import AsRan, Lead, leads_named
from threads.log import BranchId, Principal, ThreadStartedEvent
from threads.result import Err, Ok
from threads.store import SqliteStore
from threads.team.rows import member_rows, team_row


@dataclass(frozen=True, slots=True)
class OpenTeamError:
    code: Literal["not_found", "forbidden", "unavailable"]
    message: str


async def open_team(
    store: Store, ref: TeamRef, *, principal: Principal
) -> Ok[Team] | Err[OpenTeamError]:
    """A Team handle acting as `principal`: another tenant's is forbidden, a team not stored in
    `ref.tenant` is not_found, and a lead this process doesn't define is unavailable."""
    if principal.tenant != ref.tenant:
        message = f"the principal is of tenant {principal.tenant}, not the team's"
        return Err(OpenTeamError("forbidden", message))
    sq = await open_store(scoped(store, ref.tenant))
    team = await sq.run(lambda c: team_row(c, ref.id))
    if team is None or team.tenant_id != ref.tenant:
        return Err(OpenTeamError("not_found", f"no team {ref.id} in {ref.tenant}"))
    lead = await _rebound(sq, ref)
    if isinstance(lead, Err):
        return lead
    return Ok(Team(HandleEnv(sq, ref, principal, lead.value.pin, lead.value.limits)))


async def _rebound(sq: SqliteStore, ref: TeamRef) -> Ok[Lead] | Err[OpenTeamError]:
    """The lead that ran, as this process defines it: among the leads of its name, the one whose
    pin has the lead thread's config_hash, as materialize rebinds a member. Anything else would
    start agents the team never listed, so no match is unavailable and nothing is recorded."""
    rows = await sq.run(lambda c: member_rows(c, ref.id))
    row = next((r for r in rows if r.role == "lead"), None)
    if row is None or row.branch_id is None:
        raise AssertionError(f"team {ref.id} has no lead log")
    read = await sq.read(BranchId(row.branch_id), now_ms())
    if isinstance(read, Err):
        raise AssertionError(f"team {ref.id}: {read.error.message}")
    started = next(e for e in read.value.fold.events if isinstance(e, ThreadStartedEvent))
    parent = started.data.parent
    member = parent is not MISSING and parent.relation == "team_member"
    answerer = any(t.name == "ask_user" for t in started.data.tools)
    policy = started.data.policy
    context = None if policy is MISSING else policy.context
    defer = None if context is None or context is MISSING else context.defer_tools
    for lead in leads_named(row.agent):
        if await _hash_of(lead, AsRan(member, answerer, defer)) == row.config_hash:
            return Ok(lead)
    message = (
        f"this process does not define lead {row.agent} at config {row.config_hash}; "
        "define that agent here to act on its team"
    )
    return Err(OpenTeamError("unavailable", message))


async def _hash_of(lead: Lead, as_ran: AsRan) -> str | None:
    """A candidate that can't be set up here is no match."""
    try:
        return await lead.config_hash(as_ran)
    except ConfigError:
        return None
