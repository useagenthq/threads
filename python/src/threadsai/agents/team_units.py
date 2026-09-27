"""The appends on a team branch that run no loop: a failed rebind's end, and the consume of the
mail waiting for a branch nothing is running (an ended member's refusals, an idle lead's
receipts). Each takes the branch's lease, appends once and hands the lease back; a lease held
elsewhere means its holder does it."""

import uuid
from collections.abc import Sequence

from threadsai.agents.store import now_ms
from threadsai.log import BranchId
from threadsai.result import Err, Ok
from threadsai.store import Draft, SqliteStore
from threadsai.store.writer import DecideTx, Refusal
from threadsai.team.batch import Batch, Mint
from threadsai.team.close import reader_of
from threadsai.team.consume import ConsumeContext, Consumed, consume
from threadsai.team.materialize_types import RebindCode
from threadsai.team.rebind import rebind_failed
from threadsai.team.settle import AppendContext


async def end_unbound(
    sq: SqliteStore, mint: Mint | None, branch: BranchId, holder: str, code: RebindCode
) -> bool:
    """A member whose definition can't be rebound here ends failed, under its own writer. False:
    its lease is held elsewhere, and its holder does it."""
    got = await sq.acquire(branch, holder, now_ms)
    if isinstance(got, Err):
        return False
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
    return True


async def take_mail(sq: SqliteStore, mint: Mint | None, branch: BranchId) -> Consumed | None:
    """The branch's own writer takes its pending mail (design §4.7): an ended member's refusals,
    an idle member's or an idle lead's receipts, which open its turn. None: its lease is held
    elsewhere, and its holder consumes."""
    got = await sq.acquire(branch, f"team-{uuid.uuid4().hex}", now_ms)
    if isinstance(got, Err):
        return None
    w = got.value
    thread = w.fold.thread_id
    if thread is None:
        raise AssertionError("an acquired branch has a thread")
    taken: Consumed = Consumed("nothing_pending", ())

    def decide(tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
        nonlocal taken
        batch = Batch(tx.fold.seq, tx.now, mint)
        taken = consume(ConsumeContext(tx.conn, batch, thread, branch, tx.fold, reader_of(tx.read)))
        return batch.drafts

    appended = await w.append_decided(decide)
    await w.release()
    if isinstance(appended, Err):
        raise AssertionError(f"consume on {branch}: {appended.error}")
    return taken
