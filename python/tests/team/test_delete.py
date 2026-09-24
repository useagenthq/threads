"""Deleting with teams (spec/schema/README.md, "Deleting a thread"; Gate 1 §4.15): the deletion
set is a fixed point over subagent and team_member children and each doomed lead's team log,
deleted with every index row of its team in one transaction, only when nothing in it runs."""

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import JsonValue
from team.team_kit import (
    LEAD,
    MEMBER,
    TEAM,
    TEAM_LOG,
    TENANT,
    Line,
    add,
    branch_of,
    holding,
    lift_refusal,
    rechain,
    staged,
)

from threads.agents.store import Store, open_store
from threads.cli import main
from threads.log import ThreadId
from threads.result import Err, Ok
from threads.store.deletion import DeleteError, delete_tenant, delete_thread
from threads.team.rebuild import TEAM_TABLES, rebuild_team_index

CASES = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "cases"
NOW = 1_790_000_100_000
OTHER_TEAM = "0192c000-0000-7000-8000-000000000002"
WRITER = ThreadId("0192a000-0000-7000-8000-0000000000b4")
SETTLE = 3
"""team-settle-wakes-lead's threads: lead, researcher, team log."""
REBIND = 4
"""team-failed-rebind-bounces's threads: lead, researcher, writer, team log."""
PENDING = 2
"""team-tree-starting-member-pending's threads: lead and team log."""


def _remapped(raw: bytes, *, lead_parent: JsonValue = None) -> bytes:
    """The log as another team's: its threads, branches and team renumbered (b -> c), and its
    lead's thread_started given `lead_parent`."""
    text = raw.decode()
    for prefix in ("0192a000-0000-7000-8000-0000000000b", "0192b000-0000-7000-8000-0000000000b"):
        text = text.replace(prefix, prefix[:-1] + "c")
    text = text.replace(TEAM, OTHER_TEAM)

    def parented(events: list[Line]) -> list[Line]:
        data = events[0]["data"]
        assert isinstance(data, dict)
        if lead_parent is not None and events[0]["type"] == "thread_started" and "team" in data:
            events[0]["data"] = {**data, "parent": lead_parent}
        return events

    return rechain(text.encode(), parented)


async def _team(case: str) -> Store:
    store = await holding(staged(case))
    assert await rebuild_team_index(await open_store(store), TEAM) == Ok(None)
    return store


async def _delete(store: Store, thread: ThreadId) -> Ok[int] | Err[DeleteError]:
    return await (await open_store(store)).run(lambda c: delete_thread(c, TENANT, thread, NOW))


async def _count(store: Store, sql: str) -> int:
    (n,) = await (await open_store(store)).run(lambda c: c.execute(sql).fetchone())
    assert isinstance(n, int)
    return n


async def _team_rows(store: Store) -> int:
    total = 0
    for table in (*TEAM_TABLES, "team_feed"):
        total += await _count(store, f"SELECT COUNT(*) FROM {table}")  # noqa: S608
    return total


def test_a_lead_takes_its_members_team_log_and_every_row(monkeypatch: pytest.MonkeyPatch) -> None:
    lift_refusal(monkeypatch)

    async def main_() -> None:
        store = await _team("team-failed-rebind-bounces")
        assert await _team_rows(store) > 0
        assert await _delete(store, LEAD) == Ok(REBIND)
        assert await _team_rows(store) == 0
        assert await _count(store, "SELECT COUNT(*) FROM threads") == 0
        assert await _count(store, "SELECT COUNT(*) FROM tombstones") == REBIND

    asyncio.run(main_())


def test_a_starting_member_has_no_thread_to_tombstone(monkeypatch: pytest.MonkeyPatch) -> None:
    lift_refusal(monkeypatch)

    async def main_() -> None:
        store = await _team("team-tree-starting-member-pending")
        assert await _count(store, "SELECT COUNT(*) FROM mail WHERE kind = 'task'") == 1
        assert await _delete(store, LEAD) == Ok(PENDING)
        assert await _team_rows(store) == 0
        tombs = await (await open_store(store)).run(
            lambda c: {t for (t,) in c.execute("SELECT thread_id FROM tombstones")}
        )
        assert tombs == {LEAD, TEAM_LOG}

    asyncio.run(main_())


@pytest.mark.parametrize("thread", [MEMBER, WRITER, TEAM_LOG])
def test_a_member_or_team_log_alone_is_thread_in_team(
    thread: ThreadId, monkeypatch: pytest.MonkeyPatch
) -> None:
    lift_refusal(monkeypatch)

    async def main_() -> None:
        store = await _team("team-failed-rebind-bounces")
        before = await _team_rows(store)
        found = await _delete(store, thread)
        assert isinstance(found, Err)
        assert found.error.code == "thread_in_team"
        assert f"delete its lead {LEAD}" in found.error.message
        assert await _team_rows(store) == before
        assert await _count(store, "SELECT COUNT(*) FROM tombstones") == 0

    asyncio.run(main_())


@pytest.mark.parametrize(("expires_at", "outcome"), [(NOW + 1, "busy"), (NOW, "ok")])
def test_a_live_member_lease_is_busy(
    expires_at: int, outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    lift_refusal(monkeypatch)

    async def main_() -> str:
        store = await _team("team-settle-wakes-lead")
        await (await open_store(store)).run(
            lambda c: c.execute(
                "INSERT INTO leases (branch_id, holder_id, epoch, expires_at) VALUES (?, ?, ?, ?)",
                (branch_of(MEMBER), "worker", 1, expires_at),
            )
        )
        found = await _delete(store, LEAD)
        if isinstance(found, Err):
            assert "holds a live lease" in found.error.message
            assert await _count(store, "SELECT COUNT(*) FROM threads") == SETTLE
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


def test_a_nested_team_goes_whole_and_an_unrelated_team_stays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """team-tree-starting-member-pending, renumbered, is led by a member of the settle team; a
    second renumbered copy has no parent and must stay."""
    lift_refusal(monkeypatch)
    nested_parent: JsonValue = {
        "relation": "team_member",
        "thread_id": LEAD,
        "branch_id": branch_of(LEAD),
        "event_id": "0192e001-0000-7000-8000-000000000008",
    }

    async def main_() -> None:
        store = await _team("team-settle-wakes-lead")
        nested = {
            k: _remapped(v, lead_parent=nested_parent)
            for k, v in staged("team-tree-starting-member-pending").items()
        }
        await add(await open_store(store), nested)
        found = await _delete(store, LEAD)
        assert found == Ok(SETTLE + PENDING)
        assert await _count(store, "SELECT COUNT(*) FROM threads") == 0

    asyncio.run(main_())


def test_an_unrelated_team_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    lift_refusal(monkeypatch)

    async def main_() -> None:
        store = await _team("team-settle-wakes-lead")
        sq = await open_store(store)
        await add(sq, {k: _remapped(v) for k, v in staged("team-settle-wakes-lead").items()})
        assert await rebuild_team_index(sq, OTHER_TEAM) == Ok(None)
        assert await _delete(store, LEAD) == Ok(SETTLE)
        assert await _count(store, "SELECT COUNT(*) FROM threads") == SETTLE
        members = f"SELECT COUNT(*) FROM team_members WHERE team_id = '{OTHER_TEAM}'"  # noqa: S608
        assert await _count(store, members) == SETTLE - 1

    asyncio.run(main_())


def test_a_subagent_of_a_member_goes_and_a_handoff_target_lead_stays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member's subagent is a child; a lead that is a handoff target is its own lead."""
    lift_refusal(monkeypatch)
    sub_parent: JsonValue = {
        "relation": "subagent",
        "thread_id": MEMBER,
        "branch_id": branch_of(MEMBER),
        "event_id": "0192e001-0000-7000-8000-000000000003",
    }
    handoff: JsonValue = {**_obj(sub_parent), "relation": "handoff"}

    async def main_() -> None:
        store = await _team("team-settle-wakes-lead")
        sq = await open_store(store)
        sub = _remapped(staged("team-settle-wakes-lead")["researcher"])
        await add(sq, {"sub": rechain(sub, _parent_is(sub_parent))})
        target = staged("team-tree-starting-member-pending")
        await add(sq, {k: _remapped(v, lead_parent=handoff) for k, v in target.items()})
        # The researcher's subagent (renumbered c2) goes; the handoff-target team (c1, c3) stays.
        assert await _delete(store, LEAD) == Ok(SETTLE + 1)
        assert await _count(store, "SELECT COUNT(*) FROM threads") == PENDING

    asyncio.run(main_())


def _parent_is(parent: JsonValue) -> Callable[[list[Line]], list[Line]]:
    def edit(events: list[Line]) -> list[Line]:
        data = _obj(events[0]["data"])
        events[0]["data"] = {**data, "parent": parent}
        return events

    return edit


def _obj(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def test_whole_tenant_is_one_set_and_a_crash_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lift_refusal(monkeypatch)

    async def main_() -> None:
        store = await _team("team-failed-rebind-bounces")
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
        assert await _count(store, "SELECT COUNT(*) FROM threads") == REBIND
        assert await _count(store, "SELECT COUNT(*) FROM tombstones") == 0
        assert await _team_rows(store) > 0
        await sq.run(lambda c: c.execute("DROP TRIGGER crash"))
        assert await sq.run(lambda c: delete_tenant(c, TENANT, NOW)) == Ok(REBIND)
        assert await sq.run(lambda c: delete_tenant(c, "nobody", NOW)) == Ok(0)

    asyncio.run(main_())


def test_another_tenants_thread_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    lift_refusal(monkeypatch)

    async def main_() -> None:
        store = await _team("team-settle-wakes-lead")
        sq = await open_store(store)
        found = await sq.run(lambda c: delete_thread(c, "other", LEAD, NOW))
        assert found == Err(DeleteError("not_found", f"no thread {LEAD}"))

    asyncio.run(main_())


def test_the_cli_prints_the_refusal_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lift_refusal(monkeypatch)

    async def seed() -> None:
        on_disk = Store(str(tmp_path), tenant=TENANT)
        await add(await open_store(on_disk), staged("team-settle-wakes-lead"))

    asyncio.run(seed())
    assert main(["--store", str(tmp_path), "--tenant", TENANT, "delete", MEMBER]) == 1
    assert capsys.readouterr().err.startswith("thread_in_team: ")
    assert main(["--store", str(tmp_path), "--tenant", TENANT, "delete", LEAD]) == 0
    assert capsys.readouterr().out.strip() == f"deleted {LEAD} (3 threads)"
