"""A thread has one root branch (spec/schema/store.sql, branches_root): a second root is refused
as a value, stores racing to create one thread leave one root, and an import never adds one."""

import asyncio
from pathlib import Path

import pytest
from store.test_writer import CHILD, ROOT, T0, THREAD, run

from threads.result import Err, Ok
from threads.store import SqliteStore, verify_export
from threads.store.conn import Conn


def _roots(conn: Conn) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT branch_id FROM branches WHERE thread_id = ? AND parent_branch_id IS NULL",
        (THREAD,),
    ).fetchall()


def test_a_second_root_of_a_stored_thread_is_refused() -> None:
    async def test(store: SqliteStore) -> None:
        assert await store.create(THREAD, ROOT, T0) == Ok(None)
        again = await store.create(THREAD, CHILD, T0)
        assert isinstance(again, Err)
        assert again.error.code == "invalid_transition"
        assert await store.root_or_create(THREAD, CHILD, T0) == ROOT
        assert await store.run(_roots) == [(ROOT,)]

    run(test)


def test_an_import_of_another_root_of_a_stored_thread_is_refused() -> None:
    async def test(target: SqliteStore) -> None:
        source = await SqliteStore.open()
        assert isinstance(source, Ok)
        assert await source.value.create(THREAD, CHILD, T0) == Ok(None)
        exported = await source.value.export(CHILD)
        await source.value.close()
        assert isinstance(exported, Ok)
        verified = verify_export(exported.value, T0)
        assert isinstance(verified, Ok)
        assert await target.create(THREAD, ROOT, T0) == Ok(None)
        imported = await target.import_log(verified.value)
        assert isinstance(imported, Err)
        assert imported.error.code == "branch_exists"
        assert await target.run(_roots) == [(ROOT,)]

    run(test)


@pytest.mark.sqlite_only
def test_two_stores_racing_to_create_one_thread_leave_one_root(tmp_path: Path) -> None:
    # The Postgres leg of this race is tests/postgres/test_pg_races.py.
    async def main() -> None:
        path = tmp_path / "store.db"
        a, b = await SqliteStore.open(path), await SqliteStore.open(path)
        assert isinstance(a, Ok)
        assert isinstance(b, Ok)
        made = await asyncio.gather(
            a.value.create(THREAD, ROOT, T0), b.value.create(THREAD, CHILD, T0)
        )
        assert sorted("ok" if isinstance(m, Ok) else m.error.code for m in made) == [
            "invalid_transition",
            "ok",
        ]
        assert len(await a.value.run(_roots)) == 1
        await a.value.close()
        await b.value.close()

    asyncio.run(main())
