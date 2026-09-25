"""Opening a Postgres store: the schema created once across machines, the version checked.

Open takes a session-level advisory lock keyed by the schema before its transaction begins, so
the transaction's snapshot sees whatever an earlier opener committed: N processes opening one
empty database give one schema and N successful opens, and per-test schemas never wait on each
other. The version rules are SQLite's; the version is `threads_meta.store_version`.
"""

import hashlib
import time
from collections.abc import Callable
from typing import Final

from threads._generated.store_pg_sql import STORE_SQL, STORE_VERSION
from threads.log import ParseError
from threads.postgres.artifacts import PgArtifacts
from threads.postgres.driver import PgConn, connector
from threads.result import Err, Ok
from threads.store import ArtifactStore, SqliteStore
from threads.store.conn import Conn, run
from threads.store.sql import text_of
from threads.store.worker import Clock, Worker

MIN_SERVER: Final = 160000
"""Postgres 16 is the floor."""


def lock_key(schema: str) -> int:
    """The advisory lock of one schema: sha256("threads-store" || schema), first 8 bytes as a
    signed big-endian int64."""
    digest = hashlib.sha256(b"threads-store" + schema.encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _wall() -> int:
    return time.time_ns() // 1_000_000


async def open_postgres(
    url: str,
    tenant_id: str,
    *,
    connect: Callable[[], Conn] | None = None,
    clock: Clock = _wall,
    artifacts: ArtifactStore | None = None,
) -> Ok[SqliteStore] | Err[ParseError]:
    """The store on the database at `url`, scoped to one tenant. `clock` is the artifacts'
    `created_at`; `connect` and `artifacts` (in place of the `artifacts` table) are test
    seams."""
    worker = await Worker.start(connect or connector(url))
    error = await worker.free(install)
    if error is not None:
        await worker.close()
        return Err(error)
    if artifacts is None:
        artifacts = await worker.free(lambda c: PgArtifacts(c, clock))
    return Ok(SqliteStore(worker, tenant_id, artifacts))


def install(conn: Conn) -> ParseError | None:
    """Creates the schema in an empty database, else checks its version, under the lock."""
    if not isinstance(conn, PgConn):
        raise TypeError("the Postgres schema installs on a Postgres connection")
    row = conn.raw.execute("SELECT current_schema()").fetchone()
    key = lock_key("" if row is None else text_of(row[0]))
    conn.raw.execute("SELECT pg_advisory_lock(%s)", (key,))
    try:
        return run(conn, _create)
    finally:
        conn.raw.execute("SELECT pg_advisory_unlock(%s)", (key,))


def _create(conn: Conn) -> ParseError | None:
    server = int(text_of(_one(conn, "SHOW server_version_num")))
    if server < MIN_SERVER:
        version = text_of(_one(conn, "SHOW server_version"))
        return ParseError("unsupported_format", f"Postgres {version} is older than 16")
    if _one(conn, "SELECT to_regclass('threads_meta') IS NOT NULL") is not True:
        if not isinstance(conn, PgConn):
            raise TypeError("the Postgres schema installs on a Postgres connection")
        conn.raw.execute(STORE_SQL.encode())
        conn.execute(
            "INSERT INTO threads_meta (key, value) VALUES ('store_version', ?)",
            (str(STORE_VERSION),),
        )
        return None
    found = conn.execute("SELECT value FROM threads_meta WHERE key = 'store_version'").fetchone()
    version = 0 if found is None else int(text_of(found[0]))
    if version > STORE_VERSION:
        message = f"store schema {version} is newer than {STORE_VERSION}"
        return ParseError("unsupported_format", message)
    if version != STORE_VERSION:
        message = f"this store was created by an earlier threads version (schema {version});"
        return ParseError("unsupported_format", f"{message} create a new store")
    return None


def _one(conn: Conn, sql: str) -> object:
    row = conn.execute(sql).fetchone()
    return None if row is None else row[0]
