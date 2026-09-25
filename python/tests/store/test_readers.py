"""Readers never contend with writers (lane 27, review 27-r2-1): on a shared SQLite file a store
polling a branch in read-only transactions (BEGIN DEFERRED) while another store appends meets no
busy error, and a write inside a read-only transaction fails as a bug."""

import asyncio
from pathlib import Path

import pytest
from store.test_writer import DONE, ROOT, Clock, started, user

from threads.result import Ok
from threads.store import SqliteStore

pytestmark = pytest.mark.sqlite_only
APPENDS = 40


def test_a_reader_polling_during_appends_meets_no_busy(tmp_path: Path) -> None:
    path = tmp_path / "threads.db"

    async def main() -> None:
        writing, reading = await SqliteStore.open(path), await SqliteStore.open(path)
        assert isinstance(writing, Ok)
        assert isinstance(reading, Ok)
        writer = await started(writing.value, Clock())
        done = asyncio.Event()

        async def poll() -> int:
            reads = 0
            while not done.is_set():
                assert isinstance(await reading.value.read(ROOT, 0), Ok)
                reads += 1
            return reads

        polling = asyncio.create_task(poll())
        for i in range(APPENDS):
            assert isinstance(await writer.append([user(f"n{i}"), DONE]), Ok)
        done.set()
        assert await polling > 0
        await writing.value.close()
        await reading.value.close()

    asyncio.run(main())


def test_a_write_in_a_read_only_transaction_is_a_bug() -> None:
    async def main() -> None:
        opened = await SqliteStore.open()
        assert isinstance(opened, Ok)
        with pytest.raises(AssertionError, match="read-only"):
            await opened.value.run(
                lambda c: c.execute("DELETE FROM leases WHERE branch_id = ?", (ROOT,)),
                read_only=True,
            )
        await opened.value.close()

    asyncio.run(main())
