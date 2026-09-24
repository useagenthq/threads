"""team.members() (spec/api.json Team.members): a pure read of the team's rows, the lead's
included, in (name, generation) order. A settled member's result is hydrated; a label is read from
the member_started that started it (the lead's log, or the team log's for an operator's)."""

import sqlite3

from pydantic import TypeAdapter

from threads.agents.member_results import hydrated
from threads.agents.store import now_ms
from threads.agents.team_handle_types import MemberState, TeamMember
from threads.log import BranchId, MemberRef, MemberStartedEvent, StoredMemberResult
from threads.result import Err
from threads.store import SqliteStore
from threads.store.sql import int_of, text_of
from threads.team.rows import bytes_of, team_row

_RESULT: TypeAdapter[StoredMemberResult] = TypeAdapter(StoredMemberResult)
_STATES: dict[str, MemberState] = {
    "starting": "starting",
    "running": "running",
    "idle": "idle",
    "parked": "parked",
    "ended": "ended",
}

type _Row = tuple[str, int, str, MemberState, str, str | None, bytes | None]


def _rows(conn: sqlite3.Connection, team: str) -> list[_Row]:
    found: list[tuple[object, ...]] = conn.execute(
        "SELECT name, generation, agent, state, role, branch_id, result FROM team_members"
        " WHERE team_id = ? ORDER BY name, generation",
        (team,),
    ).fetchall()
    return [
        (
            text_of(name),
            int_of(gen),
            text_of(agent),
            _STATES[text_of(state)],
            text_of(role),
            None if branch is None else text_of(branch),
            None if result is None else bytes_of(result),
        )
        for name, gen, agent, state, role, branch, result in found
    ]


async def roster(sq: SqliteStore, team: str) -> tuple[TeamMember, ...]:
    """Every member of the team. Raises StoreCorruptError when a result's artifact is broken."""
    teams = await sq.run(lambda c: team_row(c, team))
    if teams is None:
        return ()
    rows = await sq.run(lambda c: _rows(c, team))
    lead = next((r[5] for r in rows if r[4] == "lead"), None)
    labels = await _labels(sq, (lead, teams.team_log_branch_id))
    out: list[TeamMember] = []
    for name, gen, agent, state, _role, _branch, raw in rows:
        ref = MemberRef(tenant=teams.tenant_id, team=team, name=name, generation=gen)
        result = (
            None if raw is None else await hydrated(_RESULT.validate_json(raw), sq.get_artifact)
        )
        out.append(TeamMember(name, ref, agent, state, result, labels.get((name, gen))))
    return tuple(out)


async def _labels(sq: SqliteStore, branches: tuple[str | None, ...]) -> dict[tuple[str, int], str]:
    """Each started member's label, by (name, generation), from the logs that start members."""
    out: dict[tuple[str, int], str] = {}
    for branch in branches:
        if branch is None:
            continue
        read = await sq.read(BranchId(branch), now_ms())
        # The index names this log: an unreadable one is a broken store, not an answer.
        if isinstance(read, Err):
            raise AssertionError(f"team log {branch}: {read.error.message}")
        for e in read.value.fold.events:
            if isinstance(e, MemberStartedEvent) and isinstance(e.data.label, str):
                out[e.data.member.name, e.data.member.generation] = e.data.label
    return out
