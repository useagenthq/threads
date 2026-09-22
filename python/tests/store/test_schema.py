"""The store's tables are exactly spec/schema/store.sql, versioned by PRAGMA user_version."""

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

from threads.log import BranchId, ThreadId
from threads.result import Err, Ok
from threads.store import LOCAL_TENANT, SqliteStore

STORE_SQL = Path(__file__).resolve().parents[3] / "spec" / "schema" / "store.sql"
THREAD = ThreadId("0192a000-0000-7000-8000-000000000001")
ROOT = BranchId("0192b000-0000-7000-8000-000000000001")
T0 = 1_790_000_000_000


def schema(conn: sqlite3.Connection) -> list[tuple[str, str, str | None]]:
    return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()


def open_store(path: Path) -> Ok[None] | Err[str]:
    """Opens the store, creates a root branch, closes it; the error code when refused."""

    async def main() -> Ok[None] | Err[str]:
        opened = await SqliteStore.open(path)
        if isinstance(opened, Err):
            return Err(opened.error.code)
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
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
        assert conn.execute("SELECT * FROM threads").fetchall() == [(THREAD, LOCAL_TENANT)]
        assert conn.execute("SELECT tenant_id FROM branches").fetchall() == [(LOCAL_TENANT,)]


def test_a_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "threads.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA user_version = 2")
    assert open_store(path) == Err("unsupported_format")
