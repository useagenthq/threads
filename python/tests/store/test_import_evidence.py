"""An import that could not verify its head keeps that evidence in storage."""

import asyncio
from pathlib import Path

from threads.log import BranchId, ThreadId
from threads.result import Err, Ok
from threads.store import SqliteStore, verify_export
from threads.store.lines import header_line

THREAD = ThreadId("0192a000-0000-7000-8000-000000000001")
ROOT = BranchId("0192b000-0000-7000-8000-000000000001")
T0 = 1_790_000_000_000
TORN = b'{"actor":{"kind":"host"},"branch_id":"0192b000-0000-7000-'
TORN_BYTES = 57
assert len(TORN) == TORN_BYTES


def imported(export: bytes, path: Path) -> None:
    """Imports `export`, then reopens the file: the evidence must survive storage."""
    verified = verify_export(export, T0)
    assert isinstance(verified, Ok)
    assert not verified.value.head_verified

    async def store_it() -> None:
        opened = await SqliteStore.open(path)
        assert isinstance(opened, Ok)
        assert await opened.value.import_log(verified.value) == Ok(None)
        await opened.value.close()

    async def reopen() -> None:
        opened = await SqliteStore.open(path)
        assert isinstance(opened, Ok)
        store = opened.value
        try:
            assert await store.export(ROOT) == Ok(export)
            read = await store.read(ROOT, T0)
            assert isinstance(read, Ok)
            assert not read.value.head_verified
            assert read.value.dropped == verified.value.dropped
            refused = await store.acquire(ROOT, "a", lambda: T0)
            assert isinstance(refused, Err)
            assert refused.error.code == "branch_not_runnable"
        finally:
            await store.close()

    asyncio.run(store_it())
    asyncio.run(reopen())


def test_a_torn_tail_is_kept_and_the_branch_stays_unverified(tmp_path: Path) -> None:
    imported(header_line(THREAD, ROOT, T0) + b"\n" + TORN, tmp_path / "threads.db")


def test_a_missing_head_line_stays_unverified(tmp_path: Path) -> None:
    imported(header_line(THREAD, ROOT, T0) + b"\n", tmp_path / "threads.db")
