"""Two branches of one team appending at once from two stores: the team feed's offsets stay
contiguous and unique per epoch (its `MAX(feed_offset) + 1` is safe under SERIALIZABLE)."""

import asyncio
from collections.abc import Iterator

import pytest
from pg_kit import Leg, need_postgres, schema_url
from team.team_kit import TEAM, TENANT, add, case_logs, verified

from threads.log import BranchId, Event, ThreadId
from threads.postgres.opening import open_postgres
from threads.result import Ok
from threads.store import SqliteStore, StoreError
from threads.store.appended import Appended
from threads.store.conn import Conn
from threads.team.rebuild import rebuild_team_index
from threads.team.write import feed_rows

ROUNDS = 25


@pytest.fixture
def leg() -> Iterator[Leg]:
    made = Leg(need_postgres())
    yield made
    made.close()


def _branch(raw: bytes) -> tuple[ThreadId, BranchId, Event]:
    read = verified(raw)
    assert isinstance(read, Ok)
    header = read.value.segments[-1].header
    return header.thread_id, header.branch_id, read.value.fold.events[-1]


async def _feed(store: SqliteStore, raw: bytes, first_seq: int) -> int:
    thread, branch, event = _branch(raw)
    done = 0
    for n in range(ROUNDS):
        one = event.model_copy(update={"seq": first_seq + n})
        a = Appended(TENANT, thread, branch, [one], frozenset(), "h", 0)
        try:
            await store.run(lambda c, a=a: feed_rows(c, a))
        except StoreError:
            continue  # an outage after the retries: nothing written
        done += 1
    return done


def _offsets(conn: Conn) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT epoch, feed_offset FROM team_feed WHERE team_id = ? ORDER BY epoch, feed_offset",
        (TEAM,),
    ).fetchall()


def test_two_branches_of_one_team_keep_the_feed_contiguous(leg: Leg) -> None:
    url = schema_url(leg.url, leg.schema())
    logs = case_logs("team-settle-wakes-lead")

    async def main() -> None:
        a = await open_postgres(url, TENANT, connect=leg.connect(url))
        b = await open_postgres(url, TENANT, connect=leg.connect(url))
        assert isinstance(a, Ok)
        assert isinstance(b, Ok)
        await add(a.value, logs)
        assert await rebuild_team_index(a.value, TEAM) == Ok(None)
        before = len(await a.value.run(_offsets, read_only=True))
        done = await asyncio.gather(
            _feed(a.value, logs["lead"], 1000), _feed(b.value, logs["researcher"], 1000)
        )
        rows = await a.value.run(_offsets, read_only=True)
        assert len(rows) == before + sum(done)
        assert [offset for _, offset in rows] == list(range(1, len(rows) + 1))
        await a.value.close()
        await b.value.close()

    asyncio.run(main())
