"""Round-2 review follow-ups: a lead that exists but whose first line doesn't parse still keeps
its member from going alone, a team id is never taken from an imported log, a large tenant stays
under SQLite's variable limit, and a rebuild refuses a forged bounce or nested task-turn mail."""

import asyncio
import sqlite3

from team.delete_kit import NOW, OTHER_TEAM, count, delete, obj, team
from team.team_kit import (
    LEAD,
    MEMBER,
    TEAM,
    TENANT,
    Line,
    branch_of,
    case_logs,
    holding,
    rechain,
)

from threads.agents.store import open_store
from threads.result import Err, Ok
from threads.store.conn import Conn
from threads.store.deletion import delete_tenant
from threads.team.rebuild import rebuild_team_index


def test_a_member_whose_lead_line_does_not_parse_stays_with_its_lead() -> None:
    """The lead's thread still exists: an unreadable first line is not a deleted lead."""

    async def main() -> None:
        store = await team("team-settle-wakes-lead")
        await (await open_store(store)).run(
            lambda c: c.execute(
                "UPDATE events SET line = CAST('{}' AS BLOB) WHERE branch_id = ? AND seq = 1",
                (branch_of(LEAD),),
            )
        )
        found = await delete(store, MEMBER)
        assert isinstance(found, Err)
        assert found.error.code == "thread_in_team"

    asyncio.run(main())


def _forged_team(events: list[Line]) -> list[Line]:
    """The team log's team_opened names another tenant's team."""
    if events[0]["type"] == "team_opened":
        events[0]["data"] = {**obj(events[0]["data"]), "team": OTHER_TEAM}
    return events


def _other_tenants_team(c: Conn) -> None:
    c.execute(
        "INSERT INTO teams (team_id, tenant_id, lead_thread_id, team_log_branch_id, closed_at)"
        " VALUES (?, 'other', '0192a000-0000-7000-8000-0000000000ef',"
        " '0192b000-0000-7000-8000-0000000000ef', NULL)",
        (OTHER_TEAM,),
    )
    c.execute(
        "INSERT INTO mail (mail_id, team_id, kind, to_name, to_generation, principal_key,"
        " root_request, envelope, created_at, state)"
        " VALUES ('m1', ?, 'message', 'x-1', 1, 'k', 'r', X'7B7D', 0, 'pending')",
        (OTHER_TEAM,),
    )


def test_a_team_log_naming_another_tenants_team_never_touches_it() -> None:
    logs = case_logs("team-settle-wakes-lead")
    logs["team"] = rechain(logs["team"], _forged_team)

    async def main() -> None:
        store = await holding(logs)
        sq = await open_store(store)
        await sq.run(_other_tenants_team)
        assert isinstance(await delete(store, LEAD), Ok)
        assert await count(store, "SELECT COUNT(*) FROM teams") == 1
        assert await count(store, "SELECT COUNT(*) FROM mail") == 1

    asyncio.run(main())


def test_a_large_tenant_stays_under_the_variable_limit() -> None:
    """Every doomed thread in one SQL list would pass the limit; none is."""
    many = 200

    def limit(c: sqlite3.Connection) -> None:
        c.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 50)

    def seed(c: Conn) -> None:
        c.executemany(
            "INSERT INTO threads (thread_id, tenant_id) VALUES (?, ?)",
            [(f"0192a000-0000-7000-8000-{n:012x}", TENANT) for n in range(many)],
        )

    async def main() -> None:
        store = await holding({})
        sq = await open_store(store)
        await sq.run_sqlite(limit)
        await sq.run(seed)
        assert await sq.run(lambda c: delete_tenant(c, TENANT, NOW)) == Ok(many)

    asyncio.run(main())


def test_a_rebuild_refuses_a_forged_bounce_and_nested_task_turn_mail() -> None:
    async def main() -> None:
        for case in ("team-bounce-provenance-rejected", "team-nested-task-turn-other-run-rejected"):
            store = await open_store(await holding(case_logs(case)))
            found = await rebuild_team_index(store, TEAM)
            assert isinstance(found, Err), case
            assert found.error.code == "invalid_transition", case

    asyncio.run(main())
