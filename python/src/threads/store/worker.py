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

from threads.store import sql

type Clock = Callable[[], int]
"""Epoch milliseconds. Injected so tests and conformance runners never read wall time."""


class Worker:
    def __init__(self, executor: ThreadPoolExecutor, conn: sqlite3.Connection) -> None:
        self._executor = executor
        self._conn = conn

    @classmethod
    async def open(cls, path: str) -> "Worker":
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="threads-store")
        loop = asyncio.get_running_loop()
        # The connection is made on the worker thread, the only thread that ever uses it.
        conn = await loop.run_in_executor(executor, sql.connect, path)
        return cls(executor, conn)

    async def call[T](self, statement: Callable[[sqlite3.Connection], T]) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, statement, self._conn)

    async def close(self) -> None:
        await self.call(sqlite3.Connection.close)
        self._executor.shutdown()
