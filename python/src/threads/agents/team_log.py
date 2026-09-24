"""The team log's writer for operator requests (design §4.3, Writes): this process's live writer of
the branch when it has one, else the lease, taken and handed back per request. A held lease is
retried with backoff until the busy bound, then busy, which writes nothing (design §4.2)."""

import asyncio
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Final, Literal

from threads.agents.store import LIVE, now_ms
from threads.log import BranchId
from threads.loop.runtime import LOST
from threads.result import Err
from threads.store import Draft, SqliteStore
from threads.store.writer import DecideTx, Refusal
from threads.team.batch import Batch, Mint
from threads.team.constants import TEAM_CONSTANTS

BUSY: Final = "busy"
_FIRST_PAUSE_MS: Final = 10


async def on_team_log[T](
    sq: SqliteStore,
    branch: BranchId,
    decide: Callable[[DecideTx, Batch], T],
    *,
    busy_bound_ms: int | None = None,
    mint: Mint | None = None,
) -> T | Literal["busy"]:
    """One decided append on the team log: `decide` adds the request's drafts to the batch and
    returns its outcome. Decided again from scratch after writer loss; busy once the bound has
    passed."""
    bound = TEAM_CONSTANTS.busy_bound_ms if busy_bound_ms is None else busy_bound_ms
    deadline = time.monotonic() + bound / 1000
    pause = _FIRST_PAUSE_MS
    while True:
        done = await _once(sq, branch, decide, mint)
        if done is not None:
            return done[0]
        left = deadline - time.monotonic()
        if left <= 0:
            return BUSY
        wait = min(pause, TEAM_CONSTANTS.wake_poll_in_process_ms) / 1000
        await asyncio.sleep(min(wait, left))
        pause *= 2


async def _once[T](
    sq: SqliteStore, branch: BranchId, decide: Callable[[DecideTx, Batch], T], mint: Mint | None
) -> tuple[T] | None:
    """One attempt: its outcome, or None when the lease was held or lost."""
    live = LIVE.get(branch)
    if live is None:
        taken = await sq.acquire(branch, uuid.uuid4().hex, now_ms)
        if isinstance(taken, Err):
            if taken.error.code == "branch_busy":
                return None
            raise AssertionError(f"team log {branch}: {taken.error.message}")
        writer = taken.value
    else:
        writer = live
    out: list[T] = []

    def run(tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
        batch = Batch(tx.fold.seq, tx.now, mint)
        out.append(decide(tx, batch))
        return batch.drafts

    try:
        done = await writer.append_decided(run)
    finally:
        if live is None:
            await writer.release()
    if isinstance(done, Refusal):
        raise AssertionError("an operator request never refuses its append")
    if isinstance(done, Err):
        if done.error.code in LOST:
            return None
        raise AssertionError(f"team log {branch}: {done.error.message}")
    return (out[-1],)
