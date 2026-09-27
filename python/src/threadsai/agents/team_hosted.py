"""What a host needs to run the team worker for a team no run of its own opened (design §7
Phase 2, "The host is a team worker"): the teams to drive, and a worker for one of them. The host
calls the store operations lane 21 built; this adds none. Mirrors TypeScript's
agent/team/hosted.ts."""

from collections.abc import Mapping
from dataclasses import dataclass

from threadsai.agents.definition import Definition
from threadsai.agents.run import member_runner
from threadsai.agents.store import Store
from threadsai.agents.team_check import agents_of
from threadsai.agents.team_supervise import HostPin
from threadsai.agents.team_worker import TeamWorker, WorkerEnv
from threadsai.agents.teams import host_member_pin, member_pin
from threadsai.store import SqliteStore
from threadsai.store.conn import Conn
from threadsai.store.sql import text_of
from threadsai.team.supervise import RestartPolicy


@dataclass(frozen=True, slots=True)
class HostedTeam:
    """An open lead team a host drives, with the lead it wakes (design §2.7.2)."""

    team_id: str
    tenant_id: str
    lead_thread_id: str
    lead_branch_id: str | None
    """None until the lead's first append; its worker has nothing to drive until then."""


@dataclass(frozen=True, slots=True)
class HostTeamRow:
    """A tenant's host team (Teams Phase 2). It has no lead, so nothing here binds one and
    nothing wakes one: only its members are driven, and its callers are woken by their own
    pending mail."""

    team_id: str
    tenant_id: str


_TEAMS = """
    SELECT t.team_id, t.tenant_id, m.thread_id, m.branch_id
      FROM teams t JOIN team_members m ON m.team_id = t.team_id AND m.role = 'lead'
     WHERE t.closed_at IS NULL AND t.kind = 'lead'
       AND NOT EXISTS (SELECT 1 FROM team_members n
                        WHERE n.thread_id = m.thread_id AND n.role = 'member')
     ORDER BY t.team_id
"""


_HOST_TEAMS = """
    SELECT team_id, tenant_id FROM teams
     WHERE kind = 'host' AND closed_at IS NULL ORDER BY team_id
"""


def hosted_teams(conn: Conn) -> list[HostedTeam]:
    """Every open lead team a host drives: the roots, whose lead is in no other team. A nested
    lead is a member of its parent's team, and that team's worker drives it (members_under)."""
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


def host_teams(conn: Conn) -> list[HostTeamRow]:
    """Every tenant's host team (Teams Phase 2), a listing of its own: it joins no lead row,
    because a host team has none."""
    rows = conn.execute(_HOST_TEAMS).fetchall()
    return [HostTeamRow(text_of(team), text_of(tenant)) for team, tenant in rows]


@dataclass(frozen=True, slots=True)
class Configured:
    """host(members=...) as a host team's worker needs it: the agents it may run, how each is
    supervised, and the pin a restart starts the next generation on."""

    agents: Mapping[str, Definition[None]]
    supervision: Mapping[str, RestartPolicy]
    pin: HostPin


def host_worker_for(
    store: Store, sq: SqliteStore, team: HostTeamRow, members: Configured
) -> TeamWorker:
    """A host team's worker: its members come from the host's own registry, pinned as host
    members, since a host team has no lead to bind or inherit from. It is the only worker that
    supervises, because only a host team has members of its own to restart (rule 51)."""
    env = WorkerEnv(
        store,
        sq,
        lambda: team.team_id,
        dict(members.agents),
        host_member_pin,
        member_runner(store),
        supervision=dict(members.supervision),
        host_pin=members.pin,
    )
    return TeamWorker(env)


def team_worker_for(
    store: Store, sq: SqliteStore, team: HostedTeam, lead: Definition[None]
) -> TeamWorker:
    """The team's worker, as a process that didn't start the team runs it: the members come from
    the lead the host binds that thread to, and materialize rebinds each one by its agent name and
    config_hash. A member this process defines differently ends failed, as in any rebind."""
    agents = agents_of(lead.team or ())
    env = WorkerEnv(store, sq, lambda: team.team_id, agents, member_pin, member_runner(store))
    return TeamWorker(env)
