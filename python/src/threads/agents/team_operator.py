"""One operator request of the Team handle (design §4.4): its key looked up under the team log's
writer, then decided by the op in the same append. Shared by every Team method that writes.
Mirrors TypeScript's agent/team/operator-request.ts."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import JsonValue

from threads.agents.store import now_ms
from threads.agents.team_handle_types import TeamRef
from threads.agents.team_log import on_team_log
from threads.agents.team_worker import WorkerEnv
from threads.agents.teams import Pin
from threads.log import BranchId, Principal
from threads.store import SqliteStore
from threads.store.lines import uuid7
from threads.store.writer import DecideTx
from threads.team.batch import Batch, Mint
from threads.team.close import CloseContext, reader_of
from threads.team.constants import TEAM_CONSTANTS
from threads.team.operator import (
    OperatorContext,
    OperatorInput,
    OperatorOp,
    Replayed,
    open_operator,
)
from threads.team.ops import TeamLimits
from threads.team.request import Request
from threads.team.rows import TeamRow, team_row


@dataclass(frozen=True, slots=True)
class HandleEnv:
    sq: SqliteStore
    ref: TeamRef
    principal: Principal
    pin: Pin
    """The team of the lead that ran, as this process defines it: the agents start resolves."""
    limits: TeamLimits
    worker: WorkerEnv
    """How the handle drives the team while an ask or a wait is open."""
    busy_bound_ms: int | None = None
    mint: Mint | None = None


type Decide = Callable[[Request, TeamRow, CloseContext], dict[str, JsonValue]]
"""An op decided in the request's append, given the append as a close (a wait may finish in it)."""


async def operator(  # noqa: PLR0913 - the request and what it decides
    env: HandleEnv,
    op: OperatorOp,
    body: Mapping[str, JsonValue],
    key: str | None,
    decide: Decide,
    *,
    big: JsonValue = None,
) -> dict[str, JsonValue] | Literal["busy"]:
    """One operator request under the team-log writer: its key looked up, then `decide` with
    the op. Returns what the op or the key's replay recorded, or busy."""
    team = await env.sq.run(lambda c: team_row(c, env.ref.id))
    if team is None:
        raise AssertionError(f"no team {env.ref.id}")
    request = OperatorInput(uuid7(now_ms()), op, env.principal, body, key)
    branch = BranchId(team.team_log_branch_id)

    def run(tx: DecideTx, batch: Batch) -> dict[str, JsonValue]:
        # Read in the append: the team may have closed since the handle looked.
        now = team_row(tx.conn, env.ref.id) or team
        ctx = OperatorContext(tx.conn, tx.fold.events, batch, lambda _text: big, now)
        opened = open_operator(ctx, request)
        if isinstance(opened, Replayed):
            return opened.outcome
        thread = tx.fold.thread_id
        if thread is None:
            raise AssertionError("a team log has a header")
        close = CloseContext(tx.conn, batch, thread, branch, tx.fold, reader_of(tx.read))
        return decide(opened.request, now, close)

    return await on_team_log(env.sq, branch, run, busy_bound_ms=env.busy_bound_ms, mint=env.mint)


async def stored_text(env: HandleEnv, text: str) -> JsonValue:
    """A text above the inline cap, stored before the append that names it; None when inline."""
    data = text.encode()
    if len(data) <= TEAM_CONSTANTS.inline_cap_bytes:
        return None
    sha = await env.sq.put_artifact(data)
    return {"sha256": sha, "bytes": len(data), "media_type": "text/plain"}


def present(body: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """A body's parameters as api.json names them, an omitted option left out."""
    return {k: v for k, v in body.items() if v is not None}


KEYED: Final = ("idempotency_key_reused", "idempotency_key_principal_mismatch")
"""The codes the team log records for an operator's key (its refusals, never a replay)."""


def code_in[C: str](codes: tuple[C, ...], done: Mapping[str, JsonValue]) -> C:
    """The refusal code the team log recorded, one of the op's."""
    code = done.get("code")
    for known in codes:
        if code == known:
            return known
    raise AssertionError(f"the team log recorded {code}")
