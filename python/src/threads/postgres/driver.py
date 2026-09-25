"""The store's connection on psycopg's sync API, owned by the store's one thread.

It runs in autocommit mode so the seam (`threads.store.conn`) issues every BEGIN and COMMIT, and
sets `default_transaction_isolation = serializable` as a second guard. psycopg's errors become
the store's: a serialization failure or deadlock is retried whole, connection loss, resource
exhaustion and shutdown are outages (StoreError), and an error from COMMIT itself is
`CommitUnknownError`. A constraint or syntax error raises as itself.
"""

from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from typing import Final

import psycopg
from psycopg import errors

from threads.agents.config import ConfigError
from threads.postgres.dsn import scrub
from threads.postgres.placeholders import psycopg as rewrite
from threads.store.conn import (
    CommitUnknownError,
    Conn,
    Cursor,
    Dialect,
    Params,
    RetryableError,
    StoreError,
)

RETRYABLE: Final = ("40001", "40P01")
OUTAGES: Final = ("08", "53", "57P0", "58")
"""SQLSTATE classes of an outage: connection exceptions, insufficient resources, shutdown and
system errors."""


def outage(error: psycopg.Error) -> bool:
    state = error.sqlstate
    # A connection psycopg itself found broken has no SQLSTATE.
    return state is None or state.startswith(OUTAGES)


@contextmanager
def _translated() -> Generator[None]:
    try:
        yield
    except psycopg.Error as error:
        if error.sqlstate in RETRYABLE:
            raise RetryableError(str(error)) from error
        if isinstance(error, psycopg.OperationalError | errors.AdminShutdown) and outage(error):
            raise StoreError(str(error)) from error
        raise


type Raw = psycopg.Connection[tuple[object, ...]]


class PgConn(Conn):
    dialect: Dialect = "postgres"

    def __init__(self, connect: Callable[[], Raw]) -> None:
        super().__init__()
        self._connect = connect
        self.raw: Raw = connect()

    def _execute(self, sql: str, params: Params) -> Cursor:
        with _translated():
            return self.raw.execute(rewrite(sql).encode(), tuple(params))

    def _executemany(self, sql: str, rows: Iterable[Params]) -> None:
        with _translated(), self.raw.cursor() as cursor:
            cursor.executemany(rewrite(sql).encode(), [tuple(row) for row in rows])

    def begin(self, *, read_only: bool) -> None:
        mode = " READ ONLY" if read_only else ""
        with _translated():
            if self.raw.closed or self.raw.broken:
                # A lost connection was an outage for the transaction on it; the next one
                # reconnects, as a restarted process would.
                self.raw = self._connect()
            self.raw.execute(f"BEGIN ISOLATION LEVEL SERIALIZABLE{mode}")

    def commit(self) -> None:
        try:
            self.raw.execute("COMMIT")
        except psycopg.Error as error:
            if error.sqlstate in RETRYABLE:
                raise RetryableError(str(error)) from error
            if outage(error):
                # Sent, and no answer: the transaction may or may not have committed.
                raise CommitUnknownError(str(error)) from error
            raise

    def rollback(self) -> None:
        if self.raw.closed or self.raw.broken:
            return
        with _translated():
            self.raw.execute("ROLLBACK")

    def close(self) -> None:
        self.raw.close()


def raw_connector(url: str) -> Callable[[], Raw]:
    """Connects on the store's thread, in autocommit, serializable by default. A server that
    can't be reached is an outage."""

    def raw() -> Raw:
        # Raised afresh and unchained: psycopg's own error may quote the connection string.
        try:
            with _translated():
                made = psycopg.connect(url, autocommit=True)
                made.execute("SET default_transaction_isolation = 'serializable'")
        except psycopg.ProgrammingError:
            message = "postgres() needs a postgres:// URL or a key=value DSN"
            raise ConfigError("invalid_config", message) from None
        except StoreError as error:
            raise StoreError(scrub(str(error), url)) from None
        return made

    return raw


def connector(url: str) -> Callable[[], PgConn]:
    return lambda: PgConn(raw_connector(url))
