"""Crashes, fencing and bytes on Postgres: a backend killed mid-append leaves no partial rows and
recovery resumes; an artifact whose append never came is only unreferenced; a writer whose lease
a machine with a clock 60 s ahead took is stale and dispatches nothing; exports move between
SQLite and Postgres byte for byte."""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from pg_drill import DrillConn, opened_with
from pg_kit import Leg, admin, need_postgres, schema_url
from store.test_writer import DONE, ROOT, T0, TTL, Clock, started, user

from threads.postgres.opening import open_postgres
from threads.result import Err, Ok
from threads.store import MemoryArtifacts, SqliteStore, StoreError, verify_export
from threads.store.conn import Cursor, Params
from threads.store.retention import referenced

CASES = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "cases"
SKEW_MS = 60_000


@pytest.fixture
def leg() -> Iterator[Leg]:
    made = Leg(need_postgres())
    yield made
    made.close()


class Killed(DrillConn):
    """Its backend is terminated at the next statement `kill_at` starts."""

    kill_at = ""

    def _execute(self, sql: str, params: Params) -> Cursor:
        if self.kill_at and sql.startswith(self.kill_at):
            self.kill_at = ""
            row = self.raw.execute("SELECT pg_backend_pid()").fetchone()
            with admin(self.url) as other:
                other.execute("SELECT pg_terminate_backend(%s)", (None if row is None else row[0],))
        return super()._execute(sql, params)


def test_a_backend_killed_mid_append_leaves_no_partial_rows_and_recovery_resumes(
    leg: Leg,
) -> None:
    async def main() -> None:
        store, conn = await opened_with(leg, Killed)
        clock = Clock()
        writer = await started(store, clock)
        before = await store.export(ROOT)
        conn.kill_at = "UPDATE branches SET head_seq"  # the events are inserted by now
        with pytest.raises(StoreError):
            await writer.append([user("lost"), DONE])
        assert await store.export(ROOT) == before  # the next transaction reconnects
        again = await store.acquire(ROOT, "a", clock)
        assert isinstance(again, Ok)
        assert isinstance(await again.value.append([user("next"), DONE]), Ok)
        await store.close()

    asyncio.run(main())


def test_an_artifact_whose_append_never_came_is_only_unreferenced(leg: Leg) -> None:
    url = schema_url(leg.url, leg.schema())

    async def main() -> None:
        first = await open_postgres(url, "local", connect=leg.connect(url))
        assert isinstance(first, Ok)
        clock = Clock()
        await started(first.value, clock)
        sha = await first.value.put_artifact(b"a spilled result")
        await first.value.close()  # the process dies before the append that names it

        second = await open_postgres(url, "local", connect=leg.connect(url))
        assert isinstance(second, Ok)
        clock.now += TTL + 1
        writer = await second.value.acquire(ROOT, "b", clock)
        assert isinstance(writer, Ok)
        assert isinstance(await writer.value.append([user("next"), DONE]), Ok)
        keep = await second.value.run(referenced, read_only=True)
        assert sha not in keep
        await second.value.close()

    asyncio.run(main())


def _awaiting_case() -> tuple[bytes, MemoryArtifacts]:
    case = CASES / "model-response-recovered-by-lookup"
    artifacts = MemoryArtifacts()
    for path in (case / "artifacts").iterdir():
        artifacts.put(path.read_bytes())
    return (case / "log.threads-py.jsonl").read_bytes(), artifacts


def test_a_takeover_by_a_clock_60s_ahead_fences_the_old_writer_and_parks_its_request(
    leg: Leg,
) -> None:
    url = schema_url(leg.url, leg.schema())
    export, artifacts = _awaiting_case()
    verified = verify_export(export, T0)
    assert isinstance(verified, Ok)
    branch = verified.value.segments[-1].header.branch_id

    async def main() -> None:
        opened = [
            await open_postgres(url, "local", connect=leg.connect(url), artifacts=artifacts)
            for _ in range(2)
        ]
        a, b = (o.value for o in opened if isinstance(o, Ok))
        assert await a.import_log(verified.value) == Ok(None)
        held = await a.acquire(branch, "a", lambda: T0)
        assert isinstance(held, Ok)
        taken = await b.acquire(branch, "b", lambda: T0 + SKEW_MS)
        assert isinstance(taken, Ok)
        # The request A sent is in doubt: B may only recover it, never re-dispatch blindly.
        assert taken.value.requires_recovery
        fenced = await held.value.fence()
        assert isinstance(fenced, Err)
        assert fenced.error.code == "stale_epoch"
        stale = await held.value.append([user("late")])
        assert isinstance(stale, Err)
        await a.close()
        await b.close()

    asyncio.run(main())


def test_exports_move_between_sqlite_and_postgres_byte_for_byte(leg: Leg) -> None:
    async def main() -> None:
        lite = await SqliteStore.open()
        back = await SqliteStore.open()
        assert isinstance(lite, Ok)
        assert isinstance(back, Ok)
        writer = await started(lite.value, Clock())
        assert isinstance(await writer.append([user("hi"), DONE]), Ok)
        exported = await lite.value.export(ROOT)
        assert isinstance(exported, Ok)
        read = verify_export(exported.value, T0)
        assert isinstance(read, Ok)

        pg, _ = await opened_with(leg, DrillConn)
        assert await pg.import_log(read.value) == Ok(None)
        from_pg = await pg.export(ROOT)
        assert from_pg == exported
        assert await back.value.import_log(read.value) == Ok(None)
        assert await back.value.export(ROOT) == from_pg
        for store in (lite.value, pg, back.value):
            await store.close()

    asyncio.run(main())
