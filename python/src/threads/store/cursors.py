"""Durable observer cursors (store.sql `observer_cursors`): how far each observer
got on a branch. Observer bookkeeping only: the log never depends on it."""

import sqlite3

from threads.log import BranchId
from threads.store.worker import Worker


class ObserverCursors:
    def __init__(self, worker: Worker) -> None:
        self._worker = worker

    async def get(self, observer: str, branch_id: BranchId) -> int:
        """The last seq `observer` handled on the branch; 0 before its first event."""

        def read(conn: sqlite3.Connection) -> int:
            row: tuple[int] | None = conn.execute(
                "SELECT seq FROM observer_cursors WHERE observer = ? AND branch_id = ?",
                (observer, branch_id),
            ).fetchone()
            return 0 if row is None else row[0]

        return await self._worker.call(read)

    async def advance(self, observer: str, branch_id: BranchId, seq: int) -> None:
        """Moves the cursor forward only: a late or repeated write never moves it back."""

        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO observer_cursors (observer, branch_id, seq) VALUES (?, ?, ?)"
                " ON CONFLICT (observer, branch_id) DO UPDATE SET seq = max(seq, excluded.seq)",
                (observer, branch_id, seq),
            )

        await self._worker.call(write)
