"""team.events(follow=True) (lane 29B): the committed feed, then new items as they commit. The
in-process wake means a follower with a very long poll still sees an append at once. Every test
ends with the team's replay check. Mirrors TypeScript's test/team/feed-follow.test.ts."""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING

import pytest
from team.run_kit import say, sq_of, start
from team.team_kit import assert_team_replays

from threads import (
    Completed,
    Failed,
    InvalidCursorError,
    Store,
    Team,
    TeamAgent,
    TeamCursor,
    TeamItem,
    agent,
    scripted_model,
    sqlite,
)
from threads.agents.team_feed import team_events
from threads.agents.team_handle_types import EpochRestarted
from threads.result import Ok
from threads.store import SqliteStore
from threads.team.rebuild import rebuild_team_index

if TYPE_CHECKING:
    from pydantic import JsonValue

NO_POLL = 60_000
"""Long enough that only the in-process wake can move these tests along."""

_FEW = 4
"""Fewer items than any lead's first run leaves in the feed."""
_AT_THE_START = 2
"""How many items the wipe test reads before it wipes the index."""


def _lead(script: "Sequence[JsonValue]") -> TeamAgent[None, str]:
    member = agent(name="writer", model=scripted_model({"responses": [say("Draft.")]}))
    return agent(name="lead", model=scripted_model({"responses": list(script)}), team=[member])


async def _ran(store: Store) -> tuple[TeamAgent[None, str], Team, SqliteStore]:
    """A lead that ran once (held, so open_team can rebind it), its team and the store."""
    lead = _lead([say("Ready."), say("Noted.")])
    r = await lead.run("Get ready.", store=store)
    assert isinstance(r, Completed)
    return lead, r.team, await sq_of(store)


def _id(item: TeamItem) -> str:
    return f"{item.cursor.epoch}:{item.cursor.offset}"


async def _take(items: AsyncGenerator[TeamItem], n: int) -> list[TeamItem]:
    """The first `n` items of a follower, then it stops iterating (the reader is killed)."""
    seen: list[TeamItem] = []
    async for item in items:
        seen.append(item)
        if len(seen) == n:
            break
    await items.aclose()
    return seen


async def _all(sq: SqliteStore, team: str, after: TeamCursor | None = None) -> list[TeamItem]:
    return [item async for item in team_events(sq, team, after)]


def test_new_items_reach_a_live_follower_woken_by_the_append_that_committed_them() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        _lead_held, team, sq = await _ran(store)
        committed = await _all(sq, team.ref.id)
        seen: list[TeamItem] = []
        drained = asyncio.Event()

        async def follow() -> None:
            async for item in team_events(sq, team.ref.id, follow=True, poll_ms=NO_POLL):
                seen.append(item)
                if len(seen) == len(committed):
                    drained.set()
                # The refused start below adds three rows; stop once they have all arrived.
                if len(seen) == len(committed) + 3:
                    return

        follower = asyncio.ensure_future(follow())
        # Only start appending once the follower has the committed feed, so the three rows below
        # can reach it only through the wake: its poll is a minute away.
        await drained.wait()
        refused = await team.start("editor", "Go.")
        assert refused.status == "refused"
        # No latency is asserted: the follower returns when it has the rows, and this bound only
        # turns a genuine hang into a failure instead of a stuck suite.
        await asyncio.wait_for(follower, 120)
        assert seen[: len(committed)] == committed
        assert len(set(map(_id, seen))) == len(seen)
        assert await _all(sq, team.ref.id) == seen
        await assert_team_replays(sq, team.ref.id)

    asyncio.run(main())


def test_a_follower_killed_mid_stream_resumes_with_no_gap_and_no_duplicate() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        _lead_held, team, sq = await _ran(store)
        whole = await _all(sq, team.ref.id)
        assert len(whole) > _FEW
        before = await _take(team_events(sq, team.ref.id, follow=True, poll_ms=NO_POLL), 3)
        resumed = await _take(
            team_events(sq, team.ref.id, before[-1].cursor, follow=True, poll_ms=NO_POLL),
            len(whole) - 3,
        )
        assert [*before, *resumed] == whole
        assert len({_id(i) for i in [*before, *resumed]}) == len(whole)
        await assert_team_replays(sq, team.ref.id)

    asyncio.run(main())


def test_an_index_wipe_mid_follow_yields_one_epoch_restarted_then_the_new_epoch() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        _lead_held, team, sq = await _ran(store)
        whole = await _all(sq, team.ref.id)
        seen: list[TeamItem] = []
        items = team_events(sq, team.ref.id, follow=True, poll_ms=NO_POLL)
        async for item in items:
            seen.append(item)
            if len(seen) == _AT_THE_START:
                assert isinstance(await rebuild_team_index(sq, team.ref.id), Ok)
            # The wiped epoch's items, then the restart, then the whole of epoch 2.
            if len(seen) == 2 * len(whole) + 1:
                break
        await items.aclose()
        restarts = [i for i in seen if isinstance(i, EpochRestarted)]
        assert restarts == [EpochRestarted(TeamCursor(2, 0))]
        after = seen[seen.index(restarts[0]) + 1 :]
        assert [i.cursor.epoch for i in after] == [2] * len(whole)
        await assert_team_replays(sq, team.ref.id)

    asyncio.run(main())


def test_a_follower_ends_when_the_team_closes_after_the_closing_appends_items() -> None:
    async def main() -> None:
        # The lead's script runs out on its wake turn: the run fails, and the lead's
        # member_ended closes its team in the same append.
        store = sqlite(":memory:")
        refused: JsonValue = {"error": {"reason": "provider_error", "http_status": 400}}
        lead = _lead([start("c1", "writer", "Go."), say("Started."), refused])
        r = await lead.run("Work.", store=store)
        assert isinstance(r, Failed)
        sq = await sq_of(store)
        team_id = r.team.ref.id
        whole = await _all(sq, team_id)
        seen = await _take(team_events(sq, team_id, follow=True, poll_ms=NO_POLL), len(whole) + 1)
        # It ended by itself, without the _take bound: a closed team ends its followers.
        assert seen == whole
        await assert_team_replays(sq, team_id)

    asyncio.run(main())


def test_a_cursor_of_a_later_epoch_or_past_the_head_is_invalid_cursor() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        _lead_held, team, sq = await _ran(store)
        whole = await _all(sq, team.ref.id)
        head = whole[-1].cursor
        # At the head is a position, not an error: it yields nothing and ends.
        assert await _all(sq, team.ref.id, head) == []
        for bad in (
            TeamCursor(head.epoch + 1, 0),
            TeamCursor(head.epoch, head.offset + 1),
            TeamCursor(head.epoch, -1),
        ):
            with pytest.raises(InvalidCursorError):
                await _all(sq, team.ref.id, bad)
        await assert_team_replays(sq, team.ref.id)

    asyncio.run(main())
