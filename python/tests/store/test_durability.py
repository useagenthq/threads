"""A file store is opened for durable commits. fullfsync only acts on darwin, but it is set on
every platform, so this reads it back everywhere."""

import contextlib
from pathlib import Path

import pytest

from threads.store.sqlite_driver import connect

pytestmark = pytest.mark.sqlite_only

BUSY_MS = 5000


def test_a_file_store_is_opened_for_durable_commits(tmp_path: Path) -> None:
    with contextlib.closing(connect(str(tmp_path / "threads.db")).raw) as conn:

        def read(pragma: str) -> object:
            return conn.execute(f"PRAGMA {pragma}").fetchone()[0]

        assert read("journal_mode") == "wal"
        assert read("synchronous") == 2  # noqa: PLR2004 - FULL
        assert read("fullfsync") == 1
        assert read("checkpoint_fullfsync") == 1
        assert read("busy_timeout") == BUSY_MS
