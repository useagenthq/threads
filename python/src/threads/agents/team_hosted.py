"""What a host needs to run the team worker for a team no run of its own opened (design §7
Phase 2, "The host is a team worker"): the teams to drive, and a worker for one of them. The host
calls the store operations lane 21 built; this adds none. Mirrors TypeScript's
agent/team/hosted.ts."""

from dataclasses import dataclass

from threads.agents.definition import Definition
from threads.agents.run import member_runner
from threads.agents.store import Store
from threads.agents.team_check import agents_of
from threads.agents.team_worker import TeamWorker, WorkerEnv
from threads.agents.teams import member_pin
from threads.store import SqliteStore
from threads.store.conn import Conn
from threads.store.sql import text_of


@dataclass(frozen=True, slots=True)
class HostedTeam:
    """An open team a host drives, with the lead it wakes (design §2.7.2)."""

    team_id: str
    tenant_id: str
    lead_thread_id: str
    lead_branch_id: str | None
    """None until the lead's first append; its worker has nothing to drive until then."""


_TEAMS = """
    SELECT t.team_id, t.tenant_id, m.thread_id, m.branch_id
      FROM teams t JOIN team_members m ON m.team_id = t.team_id AND m.role = 'lead'
     WHERE t.closed_at IS NULL AND t.kind = 'lead'
       AND NOT EXISTS (SELECT 1 FROM team_members n
                        WHERE n.thread_id = m.thread_id AND n.role = 'member')
     ORDER BY t.team_id
"""


def hosted_teams(conn: Conn) -> list[HostedTeam]:
    """Every open team a host drives: the roots, whose lead is in no other team. A nested lead is
    a member of its parent's team, and that team's worker drives it (members_under)."""
    rows = conn.execute(_TEAMS).fetchall()
    return [
        HostedTeam(
            text_of(team),
            text_of(tenant),
            text_of(thread),
            None if branch is None else text_of(branch),
        )
        for team, tenant, thread, branch in rows
    ]


def team_worker_for(
    store: Store, sq: SqliteStore, team: HostedTeam, lead: Definition[None]
) -> TeamWorker:
    """The team's worker, as a process that didn't start the team runs it: the members come from
    the lead the host binds that thread to, and materialize rebinds each one by its agent name and
    config_hash. A member this process defines differently ends failed, as in any rebind."""
    agents = agents_of(lead.team or ())
    env = WorkerEnv(store, sq, lambda: team.team_id, agents, member_pin, member_runner(store))
    return TeamWorker(env)
