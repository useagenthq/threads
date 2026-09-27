"""The store's connection seam (lane 27): one small statement surface for SQLite and Postgres,
and the transactions every statement runs in.

Every statement runs inside a transaction; there is no autocommit. A write is `BEGIN IMMEDIATE`
on SQLite and `BEGIN ISOLATION LEVEL SERIALIZABLE` on Postgres; a read-only one is `BEGIN
DEFERRED` (no write lock: readers never contend with writers) and `SERIALIZABLE READ ONLY`. A
nested transaction is a savepoint on the same connection. Statements use the portable subset of
spec/schema/README.md ("Storage"): `?` placeholders, which the Postgres driver rewrites.

The retry-safety rule: `run` may re-run a whole transaction function after a serialization
failure, so such a function reads and writes only through its connection and its own locals, and
builds everything inside the attempt from state committed before it began.
"""

import random
import sqlite3
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator, Iterable, Sequence
from contextlib import contextmanager
from typing import Final, Literal, Protocol

type Dialect = Literal["sqlite", "postgres"]
type Row = tuple[object, ...]
type Params = Sequence[object]

RETRY_BUDGET_S = 5.0
"""How long a serialization failure is retried: SQLite's busy timeout (tests shorten it)."""
FIRST_PAUSE_S: Final = 0.01
MAX_PAUSE_S: Final = 0.25


def pause_s(attempt: int, rand: Callable[[], float] = random.random) -> float:
    """The jittered pause before attempt `attempt + 1`: up to 10 ms, doubling to at most 250 ms,
    so a contended branch is retried for the whole budget without hammering the server. It
    sleeps on the store's thread, as SQLite's busy handler does."""
    return rand() * min(FIRST_PAUSE_S * 2 ** (attempt - 1), MAX_PAUSE_S)


class StoreError(Exception):
    """An outage of the store itself (SQLite busy, locked, out of space, an I/O error; a lost
    Postgres connection, a shutdown; the disk under its artifacts), raised where the store meets
    them: a later try may not meet it again. A SQL bug (a syntax error, a constraint) is never
    one: it raises as itself."""


class CommitUnknownError(StoreError):
    """`COMMIT` itself failed (the connection dropped after it was sent): the transaction may or
    may not have committed. A writer that meets it is poisoned and its owner reloads it from the
    log; every other write is keyed or conditional, so doing it again is a no-op."""


class RetryableError(Exception):
    """A serialization failure or deadlock (Postgres 40001, 40P01): nothing committed, and the
    whole transaction function may run again."""


class Cursor(Protocol):
    @property
    def rowcount(self) -> int: ...

    def fetchone(self) -> Row | None: ...

    def fetchall(self) -> list[Row]: ...


class Conn(ABC):
    """A connection the store's one thread owns. Rows come back as the driver returns them:
    unknown until parsed (storage is a boundary)."""

    dialect: Dialect

    def __init__(self) -> None:
        self.depth = 0
        """Open transactions: 0 outside one, 1 in one, more in its savepoints."""
        self.retries = 0
        """Serialization retries so far (tests assert on it)."""

    def execute(self, sql: str, params: Params = ()) -> Cursor:
        self._inside()
        return self._execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Params]) -> None:
        self._inside()
        self._executemany(sql, rows)

    def _inside(self) -> None:
        if self.depth == 0:
            raise AssertionError("a store statement runs inside a transaction")

    @abstractmethod
    def _execute(self, sql: str, params: Params) -> Cursor: ...

    @abstractmethod
    def _executemany(self, sql: str, rows: Iterable[Params]) -> None: ...

    @abstractmethod
    def begin(self, *, read_only: bool) -> None: ...

    @abstractmethod
    def commit(self) -> None: ...

    @abstractmethod
    def rollback(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...


def one(row: Row | None) -> Row:
    """The row a statement always returns (an aggregate's)."""
    if row is None:
        raise AssertionError("the statement returns a row")
    return row


@contextmanager
def transaction(conn: Conn, *, read_only: bool = False) -> Generator[None]:
    """One transaction, or a savepoint inside the open one. A raise rolls it back and passes
    through."""
    if conn.depth > 0:
        with _savepoint(conn):
            yield
        return
    conn.begin(read_only=read_only)
    conn.depth = 1
    try:
        yield
    except BaseException:
        conn.depth = 0
        conn.rollback()
        raise
    conn.depth = 0
    conn.commit()


@contextmanager
def _savepoint(conn: Conn) -> Generator[None]:
    name = f"threads_{conn.depth}"
    conn.execute(f"SAVEPOINT {name}")
    conn.depth += 1
    try:
        yield
    except BaseException:
        conn.depth -= 1
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
    conn.depth -= 1
    conn.execute(f"RELEASE SAVEPOINT {name}")


def run[T](conn: Conn, body: Callable[[Conn], T], *, read_only: bool = False) -> T:
    """`body` in one transaction, re-run whole after a serialization failure for up to 5 s;
    after that it is an outage (StoreError), as SQLite's busy timeout is. Inside an open transaction
    it is a savepoint, and the outer transaction owns the retry."""
    if conn.depth > 0:
        with transaction(conn):
            return body(conn)
    began = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            with transaction(conn, read_only=read_only):
                return body(conn)
        except RetryableError as error:
            if time.monotonic() - began >= RETRY_BUDGET_S:
                message = f"still conflicting after {attempt} attempts over {RETRY_BUDGET_S} s"
                raise StoreError(message) from error
            conn.retries += 1
            time.sleep(pause_s(attempt))


class SqliteConn(Conn):
    """stdlib sqlite3 in autocommit mode, so this seam issues every BEGIN and COMMIT."""

    dialect: Dialect = "sqlite"

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self.raw = conn
        self._query_only = False

    def _execute(self, sql: str, params: Params) -> Cursor:
        with self._writable():
            return self.raw.execute(sql, tuple(params))

    def _executemany(self, sql: str, rows: Iterable[Params]) -> None:
        with self._writable():
            self.raw.executemany(sql, (tuple(row) for row in rows))

    @contextmanager
    def _writable(self) -> Generator[None]:
        """A write inside a read-only transaction is a bug, never an outage (as on Postgres)."""
        try:
            yield
        except sqlite3.OperationalError as error:
            if self._query_only and getattr(error, "sqlite_errorname", "") == "SQLITE_READONLY":
                raise AssertionError("a write inside a read-only transaction") from error
            raise

    def begin(self, *, read_only: bool) -> None:
        self.raw.execute("BEGIN DEFERRED" if read_only else "BEGIN IMMEDIATE")
        if read_only:
            # A write inside a read-only transaction is a bug, on SQLite as on Postgres.
            self.raw.execute("PRAGMA query_only = ON")
            self._query_only = True

    def commit(self) -> None:
        self._end("COMMIT")

    def rollback(self) -> None:
        self._end("ROLLBACK")

    def _end(self, statement: str) -> None:
        try:
            self.raw.execute(statement)
        finally:
            if self._query_only:
                self._query_only = False
                self.raw.execute("PRAGMA query_only = OFF")

    def close(self) -> None:
        self.raw.close()
