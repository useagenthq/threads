"""A fork of a lead's or a member's branch shares its thread but is not the team's branch: its
appends write no team index rows and no feed rows (team fork is deferred), so the index still
equals a rebuild from the team's own logs."""

import asyncio

import pytest
from store.test_writer import SNAPSHOT, user
from team.team_kit import (
    LEAD,
    MEMBER,
    TEAM,
    TENANT,
    assert_team_replays,
    branch_of,
    case_logs,
    verified,
)
from team.writes import HOLDER, ReplayClock, reappend

from threads.log import BranchId, ThreadId
from threads.result import Ok
from threads.store import ForkRequest, SqliteStore

FORK = BranchId("0192b000-0000-7000-8000-0000000000f1")
DATA = {"reason": "snapshot", "sandbox_id": "sbx_child_01", "knowledge_policy": "pinned"}


@pytest.mark.parametrize("thread", [LEAD, MEMBER], ids=["lead", "member"])
def test_an_append_to_a_fork_writes_no_team_rows(thread: ThreadId) -> None:
    async def main() -> None:
        opened = await SqliteStore.open(tenant_id=TENANT)
        assert isinstance(opened, Ok)
        store = opened.value
        reads = [verified(raw) for raw in case_logs("team-settle-wakes-lead").values()]
        await reappend(store, [r.value for r in reads if isinstance(r, Ok)])
        clock = ReplayClock(1_790_000_100_000)
        original = await store.acquire(branch_of(thread), HOLDER, clock)
        assert isinstance(original, Ok)
        assert isinstance(await original.value.append([SNAPSHOT]), Ok)
        at = original.value.fold.seq
        forked = await store.fork(ForkRequest(branch_of(thread), at, FORK, DATA), "f", clock)
        assert isinstance(forked, Ok)
        assert forked.value is not None
        assert isinstance(await forked.value.append([user("Another way.")]), Ok)
        feed = await store.run(
            lambda c: c.execute("SELECT * FROM team_feed WHERE branch_id = ?", (FORK,)).fetchall()
        )
        assert feed == []
        await assert_team_replays(store, TEAM)
        await store.close()

    asyncio.run(main())
