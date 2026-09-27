"""The team worker's supervisor step (spec/schema/README.md, "Teams Phase 2" E): when a host
member's generation has ended with no decision naming it, the host team log's writer records one,
and a restart starts the next generation in the same append. Mirrors TypeScript's
agent/team/supervise.ts.

Only a host team has one, and only for a name the host still configures: a member whose `members`
entry is gone keeps its ended row and waits for an operator.
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence

from threadsai.agents.store import now_ms
from threadsai.log import BranchId
from threadsai.reduce.rules_super import generation_key
from threadsai.result import Err
from threadsai.store import Draft, SqliteStore
from threadsai.store.lines import uuid7
from threadsai.store.writer import DecideTx, Refusal
from threadsai.team.batch import Batch, Mint
from threadsai.team.rows import TeamRow, member_rows, team_row
from threadsai.team.supervise import RestartPolicy, SuperviseContext, supervise

type Supervision = Mapping[str, RestartPolicy]
"""`members.<name>`'s resolved restart policy, by host member name."""

type HostPin = Callable[[str], Awaitable[str | None]]
"""The agent's pin as the host holds it now, with its artifacts stored: what a restart starts the
next generation on. None for a name this host no longer defines, or whose pin fails here."""


async def supervise_host(
    sq: SqliteStore,
    team: str,
    policies: Supervision,
    mint: Mint | None,
    pin: HostPin | None = None,
) -> None:
    """One append for every ended host member generation this log has not decided on yet."""
    if not policies:
        return
    row = await sq.run(lambda c: team_row(c, team))
    if row is None or row.lead_thread_id is not None:
        return
    undecided = await _pending(sq, row, policies)
    if not undecided:
        return
    # Pinned before the writer: a pin stores artifacts, which an append's transaction may not.
    pins = {name: await pin(name) if pin is not None else None for name in undecided}
    branch = BranchId(row.team_log_branch_id)
    taken = await sq.acquire(branch, f"super-{uuid.uuid4().hex}", now_ms)
    if isinstance(taken, Err):
        return
    writer = taken.value

    def decide(tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
        batch = Batch(tx.fold.seq, tx.now, mint)
        for name in undecided:
            ctx = SuperviseContext(tx.conn, tx.fold, batch, row, uuid7(tx.now), pins[name])
            supervise(ctx, name, policies[name])
        return batch.drafts

    try:
        done = await writer.append_decided(decide)
    finally:
        await writer.release()
    # A second host that decided the same end from a fold that had gone stale is refused by rule
    # 51 and has rolled back: one decision stands, which is what exactly once means here.
    if isinstance(done, Err) and done.error.code != "invalid_transition":
        raise AssertionError(f"supervisor on {team}: {done.error.message}")


async def _pending(sq: SqliteStore, team: TeamRow, policies: Supervision) -> list[str]:
    """The configured host members whose current generation has ended with no decision on it."""
    rows = await sq.run(lambda c: member_rows(c, team.team_id))
    ended = [r for r in rows if r.role == "host_member" and r.state == "ended"]
    if not ended:
        return []
    read = await sq.read(BranchId(team.team_log_branch_id), now_ms())
    if isinstance(read, Err):
        return []
    decided = read.value.fold.host.decided
    return [
        r.name
        for r in ended
        if r.name in policies and generation_key(r.name, r.generation) not in decided
    ]
