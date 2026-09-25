"""The store's one thread.

stdlib sqlite3 and psycopg's sync connection are blocking, and a durable commit waits on fsync
(or the network). puts the store on its own thread behind a queue so the event loop never
blocks on it: one single-thread executor per store, which also runs every statement in call
order. The asyncio API is unchanged by it: a caller resumes only after its statement (and its
commit) has finished. Keeping this thread is the accepted exception to "asyncio only" (lane 27).
"""

import asyncio
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Final

from threads.store import conn as seam
from threads.store import sqlite_driver
from threads.store.conn import Conn, StoreError

type Clock = Callable[[], int]
"""Epoch milliseconds. Injected so tests and conformance runners never read wall time."""


OUTAGES: Final = (
    "SQLITE_BUSY",
    "SQLITE_LOCKED",
    "SQLITE_IOERR",
    "SQLITE_FULL",
    "SQLITE_CANTOPEN",
    "SQLITE_NOMEM",
    "SQLITE_READONLY",
    "SQLITE_CORRUPT",
    "SQLITE_NOTADB",
    "SQLITE_PROTOCOL",
)
"""SQLite's result codes for an outage (extended codes share their prefix); the TypeScript
driver uses the same list."""


def outage(error: sqlite3.Error) -> bool:
    name = getattr(error, "sqlite_errorname", None)
    return isinstance(name, str) and name.startswith(OUTAGES)


class Worker:
    def __init__(self, executor: ThreadPoolExecutor, conn: Conn) -> None:
        self._executor = executor
        self._conn = conn

    @property
    def dialect(self) -> seam.Dialect:
        return self._conn.dialect

    @classmethod
    async def open(cls, path: str) -> "Worker":
        """A SQLite store's thread and connection."""
        return await cls.start(lambda: sqlite_driver.connect(path))

    @classmethod
    async def start(cls, connect: Callable[[], Conn]) -> "Worker":
        """The thread, and the connection `connect` makes on it, the only thread that ever uses
        it."""
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="threads-store")
        loop = asyncio.get_running_loop()
        try:
            made = await loop.run_in_executor(executor, _guarded(connect))
        except BaseException:
            executor.shutdown()
            raise
        return cls(executor, made)

    async def call[T](self, statement: Callable[[Conn], T]) -> T:
        """`statement` in one write transaction (retried whole after a serialization failure)."""
        return await self.free(lambda c: seam.run(c, statement))

    async def read[T](self, statement: Callable[[Conn], T]) -> T:
        """`statement` in one read-only transaction: no write lock, and a write is a bug."""
        return await self.free(lambda c: seam.run(c, statement, read_only=True))

    async def free[T](self, job: Callable[[Conn], T]) -> T:
        """`job` on the store's thread with no transaction of its own: artifact files, and
        the driver's setup. A statement it issues opens its own transaction."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, _guarded(lambda: job(self._conn)))

    async def close(self) -> None:
        await self.free(lambda c: c.close())
        self._executor.shutdown()


def _guarded[T](job: Callable[[], T]) -> Callable[[], T]:
    """SQLite's outages raised as StoreError; the Postgres driver raises its own."""

    def run() -> T:
        try:
            return job()
        except sqlite3.Error as error:
            if outage(error):
                raise StoreError(str(error)) from error
            raise

    return run
