"""The store's non-log tables for one tenant: approvals, the inbox and channel
threads, idempotency receipts, schedule claims, and the branch listing. Each call is one
statement (or one transaction) on the store's own thread."""

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from threads.log import BranchId, ThreadId
from threads.store import approvals, inbox, receipts, schedules
from threads.store.sql import int_of, text_of
from threads.store.worker import Worker


@dataclass(frozen=True, slots=True)
class BranchRow:
    """A listed branch: `forking` and `fork_failed` rows are never listed."""

    branch_id: BranchId
    parent_branch_id: BranchId | None
    fork_at_seq: int | None
    state: str


class Tables:
    def __init__(self, worker: Worker, tenant_id: str) -> None:
        self._worker = worker
        self.tenant_id = tenant_id

    async def receipt(self, key: receipts.Key) -> receipts.Receipt | None:
        return await self._worker.call(lambda c: receipts.find(c, key))

    async def challenge(self, challenge_id: str) -> approvals.Challenge | None:
        return await self._worker.call(lambda c: approvals.find(c, self.tenant_id, challenge_id))

    async def expire(self, challenge_id: str, now: int) -> None:
        await self._worker.call(lambda c: approvals.expire(c, challenge_id, now))

    async def intake(
        self, items: Sequence[inbox.Item], now: int, new_thread: Callable[[], ThreadId]
    ) -> frozenset[ThreadId]:
        """The whole verified batch, in one transaction (step 4)."""
        tenant = self.tenant_id
        return await self._worker.call(
            lambda c: inbox.insert_batch(c, tenant, items, now, new_thread)
        )

    async def pending(self, thread_id: ThreadId) -> tuple[inbox.Row, ...]:
        return await self._worker.call(lambda c: inbox.pending(c, self.tenant_id, thread_id))

    async def discard(self, inbox_id: int) -> None:
        await self._worker.call(lambda c: inbox.discard(c, inbox_id))

    async def inbox_rows(self) -> tuple[inbox.Row, ...]:
        return await self._worker.call(lambda c: inbox.all_rows(c, self.tenant_id))

    async def conversation(self, thread_id: ThreadId) -> inbox.Conversation | None:
        return await self._worker.call(lambda c: inbox.conversation(c, self.tenant_id, thread_id))

    async def move(self, source: ThreadId, target: ThreadId) -> bool:
        return await self._worker.call(lambda c: inbox.move(c, self.tenant_id, source, target))

    async def claim(self, schedule_id: str, at: int, thread_id: ThreadId, now: int) -> bool:
        tenant = self.tenant_id
        return await self._worker.call(
            lambda c: schedules.claim(c, tenant, schedule_id, at, thread_id, now)
        )

    async def unconsumed_threads(self) -> tuple[tuple[str, ThreadId], ...]:
        """(tenant, thread) across every tenant: what a restarted host drains."""
        return await self._worker.call(inbox.unconsumed_threads)

    async def channel_threads(self) -> tuple[tuple[str, ThreadId], ...]:
        """(tenant, thread) of every channel conversation, across every tenant."""
        return await self._worker.call(inbox.channel_threads)

    async def branches(self, thread_id: ThreadId) -> tuple[BranchRow, ...]:
        return await self._worker.call(lambda c: _branches(c, self.tenant_id, thread_id))


def _branches(
    conn: sqlite3.Connection, tenant_id: str, thread_id: ThreadId
) -> tuple[BranchRow, ...]:
    rows: list[tuple[object, ...]] = conn.execute(
        "SELECT branch_id, parent_branch_id, fork_at_seq, state FROM branches"
        " WHERE thread_id = ? AND tenant_id = ? AND state NOT IN ('forking', 'fork_failed')"
        " ORDER BY rowid",
        (thread_id, tenant_id),
    ).fetchall()
    return tuple(
        BranchRow(
            BranchId(text_of(branch)),
            None if parent is None else BranchId(text_of(parent)),
            None if at is None else int_of(at),
            text_of(state),
        )
        for branch, parent, at, state in rows
    )
