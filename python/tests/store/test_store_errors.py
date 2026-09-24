"""The store raises `StoreError` for an outage a later try may not meet (can't open, busy,
locked, I/O, full) and for a disk that fails under its artifacts; a SQL bug (a syntax error, a
constraint) is not an outage and raises as itself. Missing and corrupt artifacts stay values."""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from threads.result import Err
from threads.store import StoreError
from threads.store.artifacts import FileArtifacts
from threads.store.worker import Worker


def test_an_outage_is_a_store_error_and_a_sql_bug_is_not(tmp_path: Path) -> None:
    async def main() -> None:
        with pytest.raises(StoreError):
            await Worker.open(str(tmp_path / "missing" / "dir" / "threads.db"))
        worker = await Worker.open(str(tmp_path / "threads.db"))
        try:
            await worker.call(lambda c: c.execute("CREATE TABLE t (x INTEGER PRIMARY KEY)"))
            await worker.call(lambda c: c.execute("INSERT INTO t (x) VALUES (1)"))
            with pytest.raises(sqlite3.OperationalError):
                await worker.call(lambda c: c.execute("SELEC 1"))
            with pytest.raises(sqlite3.IntegrityError):
                await worker.call(lambda c: c.execute("INSERT INTO t (x) VALUES (1)"))
        finally:
            await worker.close()

    asyncio.run(main())


def test_a_disk_that_fails_under_artifacts_is_a_store_error(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    good = FileArtifacts(tmp_path / "good")
    missing = good.get("0" * 64)
    assert isinstance(missing, Err)
    assert missing.error.code == "artifact_missing"
    # The root is a file: every read and write under it fails at the file system.
    root.write_bytes(b"")
    broken = FileArtifacts(root)
    with pytest.raises(StoreError):
        broken.put(b"bytes")
    with pytest.raises(StoreError):
        broken.get("0" * 64)
