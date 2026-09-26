"""The team worker's step for the team log (design §4.3, §4.7, §4.8, §4.12): mail to the operator
(a park notice, an operator-started member's task notification, a reply, a returned message) is
taken as a receipt, then every operator ask or wait whose deadline has passed is closed, all under
the team log's writer. Team close is a trigger of its own: once the team has closed, the step
closes every open ask cancelled. A held lease leaves it to its holder. Mirrors TypeScript's
agent/team/log-mail.ts."""

import sys
import uuid
from collections.abc import Sequence

from threads.agents.store import now_ms
from threads.log import BranchId
from threads.result import Err
from threads.store import Draft, SqliteStore
from threads.store.writer import DecideTx, Refusal
from threads.team.batch import Batch, Mint
from threads.team.close import reader_of
from threads.team.consume import ConsumeContext, consume
from threads.team.deadline import deadline, due_ids, next_deadline
from threads.team.rows import due_asks, pending_to, team_row


async def take_team_log_mail(sq: SqliteStore, team: str, mint: Mint | None) -> None:
    """Takes the team log's pending mail and runs its deadline step when either has work."""
    row = await sq.run(lambda c: team_row(c, team))
    if row is None:
        return
    branch = BranchId(row.team_log_branch_id)
    # Read before the writer: a team that closes in between only means this pass dates the
    # deadline step by the clock instead of closing every open ask, which the next pass does.
    closed = row.closed_at is not None
    if not await _work(sq, team, branch, closed=closed):
        return
    taken = await sq.acquire(branch, uuid.uuid4().hex, now_ms)
    if isinstance(taken, Err):
        return
    writer = taken.value
    thread = writer.fold.thread_id
    if thread is None:
        raise AssertionError("a team log has a header")

    def decide(tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
        batch = Batch(tx.fold.seq, tx.now, mint)
        ctx = ConsumeContext(tx.conn, batch, thread, branch, tx.fold, reader_of(tx.read))
        consume(ctx)
        by = sys.maxsize if closed else batch.now
        for ident in due_ids(tx.conn, tx.fold, branch, by):
            deadline(ctx, ident)
        return batch.drafts

    try:
        done = await writer.append_decided(decide)
    finally:
        await writer.release()
    if isinstance(done, Err):
        raise AssertionError(f"team log {team}: {done.error.message}")


async def _work(sq: SqliteStore, team: str, branch: BranchId, *, closed: bool) -> bool:
    """Mail is pending, the team closed with an ask open, or an ask or wait is due."""
    if await sq.run(lambda c: pending_to(c, team, None)):
        return True
    if closed and await sq.run(lambda c: due_asks(c, branch, sys.maxsize)):
        return True
    read = await sq.read(branch, now_ms())
    if isinstance(read, Err):
        return False
    fold = read.value.fold
    due = await sq.run(lambda c: next_deadline(c, fold, branch))
    return due is not None and due <= now_ms()
