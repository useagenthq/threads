"""What the built-in providers share: their tables live in the run's store and are searched with
SQLite FTS5 (BM25). FTS5 is a setup check."""

import re
import sqlite3
from collections.abc import Callable

from threads.agents.config import ConfigError

_WORD = re.compile(r"\w+")


def match_query(text: str) -> str | None:
    """The query as an FTS5 OR of quoted words, so user text is never FTS5 syntax; None when
    it has no words."""
    words = _WORD.findall(text)
    return " OR ".join('"' + w.replace('"', '""') + '"' for w in words) if words else None


def install(ddl: str) -> Callable[[sqlite3.Connection], None]:
    """Creates a provider's tables; a SQLite build without FTS5 is a setup error."""

    def run(conn: sqlite3.Connection) -> None:
        try:
            conn.executescript(ddl)
        except sqlite3.OperationalError as error:
            if "fts5" in str(error):
                raise ConfigError("capability_missing", "SQLite FTS5 is not available") from error
            raise

    return run


def transaction[T](body: Callable[[sqlite3.Connection], T]) -> Callable[[sqlite3.Connection], T]:
    """One BEGIN IMMEDIATE transaction on the store's thread: a check and its write are atomic."""

    def run(conn: sqlite3.Connection) -> T:
        conn.execute("BEGIN IMMEDIATE")
        try:
            value = body(conn)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return value

    return run
