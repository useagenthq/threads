"""The store's tables are exactly spec/schema/store.sql, versioned by PRAGMA user_version."""

import asyncio
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from threads._generated.store_sql import STORE_VERSION
from threads.log import BranchId, ThreadId
from threads.result import Err, Ok
from threads.store import LOCAL_TENANT, SqliteStore
from threads.store.sql import connect

STORE_SQL = Path(__file__).resolve().parents[3] / "spec" / "schema" / "store.sql"
THREAD = ThreadId("0192a000-0000-7000-8000-000000000001")
ROOT = BranchId("0192b000-0000-7000-8000-000000000001")
T0 = 1_790_000_000_000


def schema(conn: sqlite3.Connection) -> list[tuple[str, str, str | None]]:
    return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()


def open_store(path: Path) -> Ok[None] | Err[str]:
    """Opens the store, creates a root branch, closes it; the error code when refused."""
    opened = open_or_refuse(path)
    return opened if isinstance(opened, Ok) else Err(opened.error.split(":")[0])


def open_or_refuse(path: Path) -> Ok[None] | Err[str]:
    """Like open_store, with the refusal as `code: message`."""

    async def main() -> Ok[None] | Err[str]:
        opened = await SqliteStore.open(path)
        if isinstance(opened, Err):
            return Err(f"{opened.error.code}: {opened.error.message}")
        assert await opened.value.create(THREAD, ROOT, T0) == Ok(None)
        await opened.value.close()
        return Ok(None)

    return asyncio.run(main())


def test_a_fresh_store_has_exactly_the_spec_tables(tmp_path: Path) -> None:
    path = tmp_path / "threads.db"
    assert open_store(path) == Ok(None)
    with closing(sqlite3.connect(":memory:")) as spec, closing(sqlite3.connect(path)) as conn:
        spec.executescript(STORE_SQL.read_text(encoding="utf-8"))
        assert schema(conn) == schema(spec)
        assert conn.execute("PRAGMA user_version").fetchone() == (STORE_VERSION,)
        assert conn.execute("SELECT * FROM threads").fetchall() == [(THREAD, LOCAL_TENANT)]
        assert conn.execute("SELECT tenant_id FROM branches").fetchall() == [(LOCAL_TENANT,)]


def test_a_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "threads.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f"PRAGMA user_version = {STORE_VERSION + 1}")
    assert open_store(path) == Err("unsupported_format")


def test_a_store_an_earlier_version_created_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "threads.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript("CREATE TABLE threads (id TEXT PRIMARY KEY); PRAGMA user_version = 1")
    assert open_or_refuse(path) == Err(
        "unsupported_format: this store was created by an earlier threads version (schema 1);"
        " create a new store"
    )
    with closing(sqlite3.connect(path)) as conn:
        found = "SELECT name FROM sqlite_master WHERE name = 'schedule_threads'"
        assert conn.execute(found).fetchall() == []


def test_the_pending_sweep_reads_the_partial_index_and_removed_is_stored(tmp_path: Path) -> None:
    path = tmp_path / "threads.db"
    assert open_store(path) == Ok(None)
    with closing(sqlite3.connect(path)) as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT schedule_id FROM schedule_occurrences"
            " WHERE tenant_id = ? AND state = 'pending'"
            " ORDER BY occurrence_at, thread_id, schedule_id",
            ("local",),
        ).fetchall()
        assert "schedule_occurrences_pending" in str(plan)
        conn.execute(
            "INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at, state,"
            " reason, thread_id, claimed_at, logged_seq)"
            " VALUES ('local', 'daily', 1, 'skipped', 'removed', ?, 1, 2)",
            (THREAD,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at, state,"
                " thread_id, claimed_at) VALUES ('local', 'daily', 2, 'pending', ?, 1)",
                (THREAD,),
            )


def test_opening_a_fresh_store_waits_out_another_process_switching_it_to_wal(
    tmp_path: Path,
) -> None:
    """Two hosts opening one new store race on the WAL switch, which SQLite refuses busy
    without calling its busy handler (found by the F10.5 drill): opening waits it out."""
    failed: list[Exception] = []

    def opening(path: Path) -> None:
        try:
            connect(str(path)).close()
        except sqlite3.OperationalError as error:
            failed.append(error)

    for round_ in range(20):
        path = tmp_path / f"threads-{round_}.db"
        openers = [threading.Thread(target=opening, args=(path,)) for _ in range(6)]
        for opener in openers:
            opener.start()
        for opener in openers:
            opener.join()
    assert failed == []
