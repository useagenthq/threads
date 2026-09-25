"""The SQLite driver: the connection's settings and the schema install. The only module that
issues PRAGMAs (spec/schema/README.md, "Storage")."""

import sqlite3
import time
from typing import Final

from threads._generated.store_sql import STORE_SQL, STORE_VERSION
from threads.log import ParseError
from threads.store.conn import Conn, SqliteConn

WAL_TRIES: Final = 500
MIN_SQLITE: Final = (3, 39, 0)
"""`IS NOT DISTINCT FROM`, part of the portable subset, arrived in SQLite 3.39."""


def connect(path: str) -> SqliteConn:
    conn = sqlite3.connect(path, isolation_level=None)
    _wal(conn)
    # Durable ack: a commit returns only after a full sync. fullfsync
    # matters on darwin, where plain fsync doesn't flush the drive cache; elsewhere it's a no-op.
    for pragma in ("synchronous=FULL", "fullfsync=ON"):
        conn.execute(f"PRAGMA {pragma}")
    conn.execute("PRAGMA checkpoint_fullfsync=ON")
    conn.execute("PRAGMA foreign_keys=ON")
    return SqliteConn(conn)


def _wal(conn: sqlite3.Connection) -> None:
    """Switching a new file to WAL can meet another process doing the same, and SQLite answers
    that busy without calling its busy handler: retried, so two hosts can open one fresh store."""
    for tried in range(1, WAL_TRIES + 1):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as error:
            if "locked" not in str(error) or tried == WAL_TRIES:
                raise
            time.sleep(0.01)
        else:
            return


def install(conn: Conn) -> ParseError | None:
    """Creates the tables of spec/schema/store.sql on a new database. A database a newer schema
    wrote is refused, never downgraded; one an earlier version wrote is refused too, since
    stores are not migrated. A SQLite older than the portable subset needs is refused."""
    if not isinstance(conn, SqliteConn):
        raise TypeError("the SQLite schema installs on a SQLite connection")
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        message = f"SQLite {sqlite3.sqlite_version} is older than 3.39, which threads needs"
        return ParseError("unsupported_format", message)
    row = conn.raw.execute("PRAGMA user_version").fetchone()
    found = row[0] if row is not None else 0
    if not isinstance(found, int):
        raise TypeError(f"malformed user_version {found!r}")
    if found > STORE_VERSION:
        message = f"store schema {found} is newer than {STORE_VERSION}"
        return ParseError("unsupported_format", message)
    if 0 < found < STORE_VERSION:
        message = f"this store was created by an earlier threads version (schema {found});"
        return ParseError("unsupported_format", f"{message} create a new store")
    conn.raw.executescript(STORE_SQL)
    return None
