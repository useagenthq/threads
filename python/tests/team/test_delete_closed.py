"""Deletion fails closed and leaves nothing behind (spec/schema/README.md, "Deleting a thread"):
a branch that doesn't verify is busy, a team is found through its `teams` row, a deleted
background child takes its parent's wake row, and a member whose lead is gone is deletable."""

import asyncio
import sqlite3
from typing import TYPE_CHECKING

import pytest
from team.delete_kit import PENDING, SETTLE, count, delete, parent_is, remapped, team, team_rows
from team.team_kit import LEAD, MEMBER, add, branch_of, holding, lift_refusal, rechain, staged

from threads.agents.store import open_store
from threads.log import ThreadId
from threads.result import Err, Ok
from threads.store.deletion import TEAM_TABLES

if TYPE_CHECKING:
    from pydantic import JsonValue


def test_a_branch_that_does_not_verify_is_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    """It can't be proved free of an effect in doubt, so deletion refuses rather than erase it."""
    lift_refusal(monkeypatch)
    member = branch_of(MEMBER)

    async def main() -> None:
        store = await team("team-settle-wakes-lead")
        await (await open_store(store)).run(
            lambda c: c.execute(
                "UPDATE events SET line = CAST('{}' AS BLOB) WHERE branch_id = ? AND seq = 3",
                (member,),
            )
        )
        found = await delete(store, LEAD)
        assert isinstance(found, Err)
        assert found.error.code == "busy"
        assert f"branch {member} doesn't verify (" in found.error.message
        assert f"run `threads repair {member}` if its tail is torn" in found.error.message
        assert await count(store, "SELECT COUNT(*) FROM threads") == SETTLE

    asyncio.run(main())


def test_a_leads_team_is_found_through_its_teams_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rows go even when the lead's own log names another team id than its `teams` row."""
    lift_refusal(monkeypatch)
    renamed = "0192c000-0000-7000-8000-00000000000a"

    def rename(c: sqlite3.Connection) -> None:
        for table in TEAM_TABLES:
            c.execute(f"UPDATE {table} SET team_id = ?", (renamed,))  # noqa: S608

    async def main() -> None:
        store = await team("team-tree-starting-member-pending")
        await (await open_store(store)).run(rename)
        assert await delete(store, LEAD) == Ok(PENDING)
        assert await team_rows(store) == 0

    asyncio.run(main())


def test_deleting_a_background_child_drops_its_parents_wake_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lift_refusal(monkeypatch)
    kid = ThreadId("0192a000-0000-7000-8000-0000000000c2")
    sub_parent: JsonValue = {
        "relation": "subagent",
        "thread_id": MEMBER,
        "branch_id": branch_of(MEMBER),
        "event_id": "0192e001-0000-7000-8000-000000000003",
    }

    async def main() -> None:
        store = await team("team-settle-wakes-lead")
        sq = await open_store(store)
        child = remapped(staged("team-settle-wakes-lead")["researcher"])
        await add(sq, {"child": rechain(child, parent_is(sub_parent))})
        await sq.run(
            lambda c: c.execute(
                "INSERT INTO pending_wakes (branch_id, child_thread_id) VALUES (?, ?)",
                (branch_of(MEMBER), kid),
            )
        )
        assert await delete(store, kid) == Ok(1)
        assert await count(store, "SELECT COUNT(*) FROM pending_wakes") == 0

    asyncio.run(main())


def test_a_member_whose_lead_is_gone_can_be_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    lift_refusal(monkeypatch)

    async def main() -> None:
        store = await holding({"researcher": staged("team-settle-wakes-lead")["researcher"]})
        assert await delete(store, MEMBER) == Ok(1)

    asyncio.run(main())
