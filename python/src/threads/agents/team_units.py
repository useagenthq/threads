"""The team worker's appends on a member branch that run no loop: a failed rebind's end, and an
ended member refusing the mail that still reaches it. Each takes the member's lease, appends once
and hands the lease back; a lease held elsewhere means its holder does it."""

import uuid
from collections.abc import Sequence

from threads.agents.store import now_ms
from threads.log import BranchId
from threads.result import Err, Ok
from threads.store import Draft, SqliteStore
from threads.store.writer import DecideTx, Refusal
from threads.team.batch import Batch, Mint
from threads.team.close import reader_of
from threads.team.consume import ConsumeContext, consume
from threads.team.materialize import RebindCode
from threads.team.rebind import rebind_failed
from threads.team.settle import AppendContext


async def end_unbound(
    sq: SqliteStore, mint: Mint | None, branch: BranchId, holder: str, code: RebindCode
) -> None:
    """A member whose definition can't be rebound here ends failed, under its own writer."""
    got = await sq.acquire(branch, holder, now_ms)
    # Held elsewhere: its holder runs it.
    if isinstance(got, Err):
        return
    w = got.value
    thread = w.fold.thread_id
    if thread is None:
        raise AssertionError("an acquired branch has a thread")

    def decide(tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
        batch = Batch(tx.fold.seq, tx.now, mint)
        rebind_failed(AppendContext(tx.conn, batch, thread, branch), tx.fold, code, tx.now)
        return batch.drafts

    ended = await w.append_decided(decide)
    await w.release()
    if not isinstance(ended, Ok):
        raise AssertionError(f"member end: {ended}")


async def refuse_ended(sq: SqliteStore, mint: Mint | None, branch: BranchId) -> None:
    """An ended member's writer refuses the mail that still reaches it."""
    got = await sq.acquire(branch, f"team-{uuid.uuid4().hex}", now_ms)
    if isinstance(got, Err):
        return
    w = got.value
    thread = w.fold.thread_id
    if thread is None:
        raise AssertionError("an acquired branch has a thread")

    def decide(tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
        batch = Batch(tx.fold.seq, tx.now, mint)
        consume(ConsumeContext(tx.conn, batch, thread, branch, tx.fold, reader_of(tx.read)))
        return batch.drafts

    await w.append_decided(decide)
    await w.release()
