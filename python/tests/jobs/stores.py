"""The drills' one store, on the engine THREADS_TEST_STORE names (lane 27). On SQLite it is the
file `<dir>/threads.db`; on Postgres it is a schema named after `<dir>`, which every process of
the drill opens with its own connection and no shared disk (artifacts are rows): the
multi-machine claim. `hooked` stores call `on_statement(sql, params)` before each statement, the
drills' way to stop a process mid-transaction on either engine."""

import contextlib
import hashlib
import sqlite3
from collections.abc import Callable
from pathlib import Path

from pg_kit import ENGINE, URL, admin, drop_schema, schema_url

from threads.agents.store import Store, sqlite
from threads.log import ParseError
from threads.postgres.driver import PgConn, raw_connector
from threads.postgres.opening import open_postgres
from threads.postgres.placeholders import psycopg as rewrite
from threads.result import Err, Ok
from threads.store import SqliteStore
from threads.store.conn import Cursor, Params

type OnStatement = Callable[[str, Params], None]


def _schema(where: Path) -> str:
    return "d_" + hashlib.sha256(str(where.resolve()).encode()).hexdigest()[:20]


def _url(where: Path) -> str:
    if URL is None:
        raise AssertionError("THREADS_TEST_STORE=postgres needs THREADS_TEST_POSTGRES_URL")
    with admin(URL) as conn:
        # Processes of one drill race to make it: one statement, one winner, the rest no-ops.
        conn.execute("SELECT pg_advisory_lock(27)")
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {_schema(where)}".encode())
        conn.execute("SELECT pg_advisory_unlock(27)")
    return schema_url(URL, _schema(where))


class _Hooked(PgConn):
    def __init__(self, url: str, on_statement: OnStatement) -> None:
        super().__init__(raw_connector(url))
        self._on = on_statement

    def _execute(self, sql: str, params: Params) -> Cursor:
        self._on(sql, params)
        return super()._execute(sql, params)


async def _open(
    where: Path, tenant: str, on_statement: OnStatement | None
) -> Ok[SqliteStore] | Err[ParseError]:
    url = _url(where)
    hook = on_statement
    connect = None if hook is None else (lambda: _Hooked(url, hook))
    return await open_postgres(url, tenant, connect=connect)


def drill_store(where: Path, on_statement: OnStatement | None = None) -> Store:
    """The public Store handle every drill process uses."""
    if ENGINE != "postgres":
        return sqlite(str(where))

    async def opener(tenant: str) -> Ok[SqliteStore] | Err[ParseError]:
        return await _open(where, tenant, on_statement)

    return Store(f"postgres:{_schema(where)}", opener=opener)


async def drill_open(
    where: Path, tenant: str, on_statement: OnStatement | None = None
) -> Ok[SqliteStore] | Err[ParseError]:
    """The store itself, for one tenant."""
    if ENGINE != "postgres":
        return await SqliteStore.open(where / "threads.db", tenant_id=tenant)
    return await _open(where, tenant, on_statement)


def query(where: Path, sql: str, *params: object) -> list[tuple[object, ...]]:
    """One statement behind the processes' backs (`?` placeholders), committed."""
    if ENGINE != "postgres":
        with contextlib.closing(sqlite3.connect(where / "threads.db")) as db, db:
            return db.execute(sql, params).fetchall()
    with admin(_url(where)) as conn:
        cursor = conn.execute(rewrite(sql).encode(), params)
        return cursor.fetchall() if cursor.description is not None else []


def activity() -> str:
    """The server's sessions, for a drill that ran out of time: who waits on what."""
    if ENGINE != "postgres" or URL is None:
        return ""
    with admin(URL) as conn:
        rows = conn.execute(
            "SELECT pid, state, wait_event_type, wait_event, now() - xact_start, left(query, 160)"
            " FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
        ).fetchall()
    return "\n".join(" | ".join(map(str, row)) for row in rows)


def drop(where: Path) -> None:
    if ENGINE == "postgres" and URL is not None:
        drop_schema(URL, _schema(where))
