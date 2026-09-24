"""The team worker's step for the team log (design §4.3, §4.7): mail to the operator (a park
notice, an operator-started member's task notification, a returned message) is taken as a receipt
under the team log's writer. A held lease leaves it to its holder."""

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
from threads.team.rows import pending_to, team_row


async def take_team_log_mail(sq: SqliteStore, team: str, mint: Mint | None) -> None:
    """Consumes the team log's pending mail when there is any and its lease is free."""
    row = await sq.run(lambda c: team_row(c, team))
    if row is None or not await sq.run(lambda c: pending_to(c, team, None)):
        return
    branch = BranchId(row.team_log_branch_id)
    taken = await sq.acquire(branch, uuid.uuid4().hex, now_ms)
    if isinstance(taken, Err):
        return
    writer = taken.value
    thread = writer.fold.thread_id
    if thread is None:
        raise AssertionError("a team log has a header")

    def decide(tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
        batch = Batch(tx.fold.seq, tx.now, mint)
        consume(ConsumeContext(tx.conn, batch, thread, branch, tx.fold, reader_of(tx.read)))
        return batch.drafts

    try:
        done = await writer.append_decided(decide)
    finally:
        await writer.release()
    if isinstance(done, Err):
        raise AssertionError(f"team log {team}: {done.error.message}")
