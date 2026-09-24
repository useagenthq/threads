"""The store's one thread.

stdlib sqlite3 is blocking and a durable commit waits on fsync. puts the store on its
own thread behind a queue so the event loop never blocks on it: one single-thread executor per
store, which also runs every statement in call order. The asyncio API is unchanged by it: a
caller resumes only after its statement (and its commit) has finished.
"""

import asyncio
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Final

from threads.store import sql

type Clock = Callable[[], int]
"""Epoch milliseconds. Injected so tests and conformance runners never read wall time."""


class StoreError(Exception):
    """An outage of the store itself (SQLite busy, locked, out of space, an I/O error; the disk
    under its artifacts), raised where the store meets them: a later try may not meet it again.
    A SQL bug (a syntax error, a constraint) is never one: it raises as itself."""


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
    def __init__(self, executor: ThreadPoolExecutor, conn: sqlite3.Connection) -> None:
        self._executor = executor
        self._conn = conn

    @classmethod
    async def open(cls, path: str) -> "Worker":
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="threads-store")
        loop = asyncio.get_running_loop()
        # The connection is made on the worker thread, the only thread that ever uses it.
        try:
            conn = await loop.run_in_executor(executor, sql.connect, path)
        except sqlite3.Error as error:
            executor.shutdown()
            if outage(error):
                raise StoreError(str(error)) from error
            raise
        return cls(executor, conn)

    async def call[T](self, statement: Callable[[sqlite3.Connection], T]) -> T:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._executor, statement, self._conn)
        except sqlite3.Error as error:
            if outage(error):
                raise StoreError(str(error)) from error
            raise

    async def close(self) -> None:
        await self.call(sqlite3.Connection.close)
        self._executor.shutdown()
