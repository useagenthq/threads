"""Artifacts as rows: re-hashed on every read, shared by every store on the database, and swept
by gc only outside the grace window of the putting process's clock (a re-put inside it keeps the
artifact on every machine)."""

import asyncio
from collections.abc import Iterator

import pytest
from pg_kit import Leg, admin, need_postgres, schema_url

from threads.log.digest import sha256_hex
from threads.postgres.artifacts import PgArtifacts, candidates, delete_older
from threads.postgres.opening import install, open_postgres
from threads.result import Err, Ok
from threads.store import SqliteStore
from threads.store.conn import run

T0 = 1_790_000_000_000
DATA = b"a spilled result"
SHA = sha256_hex(DATA)


@pytest.fixture
def leg() -> Iterator[Leg]:
    made = Leg(need_postgres())
    yield made
    made.close()


class Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


async def _store(leg: Leg, url: str, clock: Clock) -> SqliteStore:
    opened = await open_postgres(url, "local", connect=leg.connect(url), clock=clock)
    assert isinstance(opened, Ok)
    return opened.value


def _created_at(url: str) -> object:
    with admin(url) as conn:
        row = conn.execute("SELECT created_at FROM artifacts WHERE sha256 = %s", (SHA,)).fetchone()
    return None if row is None else row[0]


def test_a_second_store_reads_what_the_first_put_and_a_bad_row_is_never_served(
    leg: Leg,
) -> None:
    url = schema_url(leg.url, leg.schema())

    async def main() -> None:
        a, b = await _store(leg, url, Clock(T0)), await _store(leg, url, Clock(T0))
        assert await a.put_artifact(DATA) == SHA
        assert await b.get_artifact(SHA) == Ok(DATA)
        with admin(url) as conn:
            conn.execute("UPDATE artifacts SET bytes = %s", (b"tampered",))
        corrupt = await b.get_artifact(SHA)
        assert isinstance(corrupt, Err)
        assert corrupt.error.code == "artifact_corrupt"
        with admin(url) as conn:
            conn.execute("DELETE FROM artifacts")
        missing = await b.get_artifact(SHA)
        assert isinstance(missing, Err)
        assert missing.error.code == "artifact_missing"
        spill = await a.spill()
        await spill.write(DATA[:5])
        await spill.write(DATA[5:])
        assert await spill.commit() == SHA
        assert await b.get_artifact(SHA) == Ok(DATA)
        await a.close()
        await b.close()

    asyncio.run(main())


def test_a_re_put_refreshes_created_at(leg: Leg) -> None:
    url = schema_url(leg.url, leg.schema())

    async def main() -> None:
        clock = Clock(T0)
        store = await _store(leg, url, clock)
        await store.put_artifact(DATA)
        assert _created_at(url) == T0
        clock.now = T0 + 5
        await store.put_artifact(DATA)
        assert _created_at(url) == T0 + 5
        await store.close()

    asyncio.run(main())


def test_gc_on_another_store_keeps_an_artifact_inside_the_window_and_sweeps_it_after(
    leg: Leg,
) -> None:
    url = schema_url(leg.url, leg.schema())

    async def main() -> None:
        putting, sweeping = await _store(leg, url, Clock(T0)), await _store(leg, url, Clock(T0))
        await putting.put_artifact(DATA)  # its append hasn't committed yet: nothing names it
        assert await sweeping.sweep_artifacts(frozenset(), T0) == ()
        assert await putting.get_artifact(SHA) == Ok(DATA)
        assert await sweeping.sweep_artifacts(frozenset({SHA}), T0 + 1) == ()
        assert await sweeping.sweep_artifacts(frozenset(), T0 + 1) == (SHA,)
        missing = await putting.get_artifact(SHA)
        assert isinstance(missing, Err)
        await putting.close()
        await sweeping.close()

    asyncio.run(main())


def test_a_re_put_between_gcs_read_and_its_delete_keeps_the_artifact(leg: Leg) -> None:
    url = schema_url(leg.url, leg.schema())
    conn = leg.connect(url)()
    assert install(conn) is None
    clock = Clock(T0)
    artifacts = PgArtifacts(conn, clock)
    artifacts.put(DATA)
    page = run(conn, lambda c: candidates(c, older=T0 + 1, after=""), read_only=True)
    assert page == [SHA]
    clock.now = T0 + 10
    artifacts.put(DATA)  # another machine re-puts inside the window
    assert run(conn, lambda c: delete_older(c, doomed=page, older=T0 + 1)) == []
    assert artifacts.get(SHA) == Ok(DATA)
