"""Deleting with teams (spec/schema/README.md, "Deleting a thread"; Gate 1 §4.15): the deletion
set is a fixed point over subagent and team_member children and each doomed lead's team log,
deleted with every index row of its team in one transaction, only when nothing in it runs."""

import asyncio
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from team.delete_kit import (
    CASES,
    NOW,
    OTHER_TEAM,
    PENDING,
    REBIND,
    SETTLE,
    WRITER,
    count,
    delete,
    obj,
    plain_child,
    remapped,
    team,
    team_rows,
)
from team.team_kit import (
    LEAD,
    MEMBER,
    TEAM_LOG,
    TENANT,
    add,
    branch_of,
    case_logs,
    holding,
    rebuild_all,
)

from threads.agents.store import Store, open_store
from threads.cli import main
from threads.log import ThreadId
from threads.result import Err, Ok
from threads.store.deletion import DeleteError, delete_tenant, delete_thread
from threads.team.rebuild import rebuild_team_index

if TYPE_CHECKING:
    from pydantic import JsonValue


def test_a_lead_takes_its_members_team_log_and_every_row() -> None:

    async def main_() -> None:
        store = await team("team-failed-rebind-bounces")
        assert await team_rows(store) > 0
        assert await delete(store, LEAD) == Ok(REBIND)
        assert await team_rows(store) == 0
        assert await count(store, "SELECT COUNT(*) FROM threads") == 0
        assert await count(store, "SELECT COUNT(*) FROM tombstones") == REBIND

    asyncio.run(main_())


def test_a_starting_member_has_no_thread_to_tombstone() -> None:

    async def main_() -> None:
        store = await team("team-tree-starting-member-pending")
        assert await count(store, "SELECT COUNT(*) FROM mail WHERE kind = 'task'") == 1
        assert await delete(store, LEAD) == Ok(PENDING)
        assert await team_rows(store) == 0
        tombs = await (await open_store(store)).run(
            lambda c: {t for (t,) in c.execute("SELECT thread_id FROM tombstones")}
        )
        assert tombs == {LEAD, TEAM_LOG}

    asyncio.run(main_())


@pytest.mark.parametrize("thread", [MEMBER, WRITER, TEAM_LOG])
def test_a_member_or_team_log_alone_is_thread_inteam(thread: ThreadId) -> None:

    async def main_() -> None:
        store = await team("team-failed-rebind-bounces")
        before = await team_rows(store)
        found = await delete(store, thread)
        assert isinstance(found, Err)
        assert found.error.code == "thread_in_team"
        assert f"delete its lead {LEAD}" in found.error.message
        assert await team_rows(store) == before
        assert await count(store, "SELECT COUNT(*) FROM tombstones") == 0

    asyncio.run(main_())


@pytest.mark.parametrize(("expires_at", "outcome"), [(NOW + 1, "busy"), (NOW, "ok")])
def test_a_live_member_lease_is_busy(expires_at: int, outcome: str) -> None:

    async def main_() -> str:
        store = await team("team-settle-wakes-lead")
        await (await open_store(store)).run(
            lambda c: c.execute(
                "INSERT INTO leases (branch_id, holder_id, epoch, expires_at) VALUES (?, ?, ?, ?)",
                (branch_of(MEMBER), "worker", 1, expires_at),
            )
        )
        found = await delete(store, LEAD)
        if isinstance(found, Err):
            assert "holds a live lease" in found.error.message
            assert await count(store, "SELECT COUNT(*) FROM threads") == SETTLE
            return found.error.code
        return "ok"

    assert asyncio.run(main_()) == outcome


def test_an_effect_in_doubt_is_busy() -> None:
    case = CASES / "effect-crash-after-begin-idempotent"

    async def main_() -> None:
        store = await holding({"log": (case / "log.threads-py.jsonl").read_bytes()}, "local")
        (thread,) = await (await open_store(store)).run(
            lambda c: c.execute("SELECT thread_id FROM threads").fetchone()
        )
        sq = await open_store(store)
        found = await sq.run(lambda c: delete_thread(c, "local", ThreadId(thread), NOW))
        assert isinstance(found, Err)
        assert found.error.code == "busy"
        assert "effect in doubt" in found.error.message

    asyncio.run(main_())


def test_a_nested_team_goes_whole() -> None:
    """team-nested-lead-rows: researcher-1 leads its own team; deleting the outer lead takes the
    nested lead, its team log and both teams' rows."""

    async def main_() -> None:
        logs = case_logs("team-nested-lead-rows")
        store = await holding(logs)
        await rebuild_all(await open_store(store), logs)
        assert await delete(store, LEAD) == Ok(len(logs))
        assert await count(store, "SELECT COUNT(*) FROM threads") == 0
        assert await team_rows(store) == 0

    asyncio.run(main_())


def test_an_unrelated_team_is_untouched() -> None:

    async def main_() -> None:
        store = await team("team-settle-wakes-lead")
        sq = await open_store(store)
        await add(sq, {k: remapped(v) for k, v in case_logs("team-settle-wakes-lead").items()})
        assert await rebuild_team_index(sq, OTHER_TEAM) == Ok(None)
        assert await delete(store, LEAD) == Ok(SETTLE)
        assert await count(store, "SELECT COUNT(*) FROM threads") == SETTLE
        members = f"SELECT COUNT(*) FROM team_members WHERE team_id = '{OTHER_TEAM}'"  # noqa: S608
        assert await count(store, members) == SETTLE - 1

    asyncio.run(main_())


def test_a_subagent_of_a_member_goes_and_a_handoff_target_lead_stays() -> None:
    """A member's subagent is a child; a lead that is a handoff target is its own lead."""
    sub_parent: JsonValue = {
        "relation": "subagent",
        "thread_id": MEMBER,
        "branch_id": branch_of(MEMBER),
        "event_id": "0192e001-0000-7000-8000-000000000003",
    }
    handoff: JsonValue = {**obj(sub_parent), "relation": "handoff"}

    async def main_() -> None:
        store = await team("team-settle-wakes-lead")
        sq = await open_store(store)
        await add(sq, {"sub": plain_child(sub_parent)})
        target = case_logs("team-tree-starting-member-pending")
        await add(sq, {k: remapped(v, lead_parent=handoff) for k, v in target.items()})
        # The researcher's subagent goes; the handoff-target team (c1, c3) stays.
        assert await delete(store, LEAD) == Ok(SETTLE + 1)
        assert await count(store, "SELECT COUNT(*) FROM threads") == PENDING

    asyncio.run(main_())


def test_whole_tenant_is_one_set_and_a_crash_deletes_nothing() -> None:

    async def main_() -> None:
        store = await team("team-failed-rebind-bounces")
        sq = await open_store(store)
        await sq.run(
            lambda c: c.execute(
                "CREATE TRIGGER crash BEFORE INSERT ON tombstones"
                " WHEN (SELECT COUNT(*) FROM tombstones) >= 1"
                " BEGIN SELECT RAISE(ABORT, 'crash'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError):
            await sq.run(lambda c: delete_tenant(c, TENANT, NOW))
        assert await count(store, "SELECT COUNT(*) FROM threads") == REBIND
        assert await count(store, "SELECT COUNT(*) FROM tombstones") == 0
        assert await team_rows(store) > 0
        await sq.run(lambda c: c.execute("DROP TRIGGER crash"))
        assert await sq.run(lambda c: delete_tenant(c, TENANT, NOW)) == Ok(REBIND)
        assert await sq.run(lambda c: delete_tenant(c, "nobody", NOW)) == Ok(0)

    asyncio.run(main_())


def test_another_tenants_thread_is_not_found() -> None:

    async def main_() -> None:
        store = await team("team-settle-wakes-lead")
        sq = await open_store(store)
        found = await sq.run(lambda c: delete_thread(c, "other", LEAD, NOW))
        assert found == Err(DeleteError("not_found", f"no thread {LEAD}"))

    asyncio.run(main_())


def test_the_cli_prints_the_refusal_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:

    async def seed() -> None:
        on_disk = Store(str(tmp_path), tenant=TENANT)
        await add(await open_store(on_disk), case_logs("team-settle-wakes-lead"))

    asyncio.run(seed())
    assert main(["--store", str(tmp_path), "--tenant", TENANT, "delete", MEMBER]) == 1
    assert capsys.readouterr().err.startswith("thread_in_team: ")
    assert main(["--store", str(tmp_path), "--tenant", TENANT, "delete", LEAD]) == 0
    assert capsys.readouterr().out.strip() == f"deleted {LEAD} (3 threads)"
