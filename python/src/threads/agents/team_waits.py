"""team.ask, team.wait and team.cancel (spec/api.json Team): operator requests decided by the
model tools' ops; ask and wait then return the outcome the team log records, driving the team
until it does, as a lead's run does: its members materialize and run, and the team log takes its
mail and runs its deadline step. Mirrors TypeScript's agent/team/waits.ts and drive.ts."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from typing import Final

from pydantic import JsonValue

from threads.agents.team_answers import (
    AskOutcome,
    AskRefusal,
    CancelRefusal,
    CancelRequested,
    ObserveRefusal,
    Waited,
)
from threads.agents.team_budgets import recipient_of
from threads.agents.team_handle_types import (
    OperatorRefusal,
    TeamAskRefused,
    TeamAskResult,
    TeamCancelRefused,
    TeamCancelResult,
    TeamWaitRefused,
    TeamWaitResult,
)
from threads.agents.team_log import BUSY
from threads.agents.team_operator import KEYED, HandleEnv, code_in, operator, stored_text
from threads.agents.team_outcomes import ask_outcome, wait_outcome
from threads.agents.team_worker import TeamWorker
from threads.log import MemberRef
from threads.loop.budget import ask_room
from threads.pos_int import is_pos_int
from threads.reduce.handlers import to_json
from threads.team.ask import AskPlan, open_ask
from threads.team.cancel import request_cancel
from threads.team.close import CloseContext
from threads.team.constants import TEAM_CONSTANTS
from threads.team.operator import ref_target
from threads.team.request import Request
from threads.team.rows import TeamRow, member_named
from threads.team.watch import WaitMode, open_wait, wait_members


async def ask_member(
    env: HandleEnv, to: MemberRef, question: str, timeout_ms: int | None, key: str | None
) -> TeamAskResult:
    # Refused before any writer, so, like busy, it records nothing.
    if timeout_ms is not None and not is_pos_int(timeout_ms):
        return TeamAskRefused("invalid_request")
    room = await _room(env, to)
    plan = AskPlan(env.limits, lambda _row: room, _timeout(timeout_ms))
    body: dict[str, JsonValue] = {
        "to": to.model_dump(mode="json"),
        "question": question,
        "timeout_ms": timeout_ms,
    }
    done = await operator(
        env,
        "ask",
        body,
        key,
        lambda req, team, _c: open_ask(req, ref_target(req.conn, team, to), question, plan),
        big=await stored_text(env, question),
    )
    if done == BUSY:
        return TeamAskRefused("busy")
    if done.get("status") == "refused":
        return TeamAskRefused(code_in(_ASK, done))
    ask_id = done.get("ask_id")
    if done.get("status") != "open" or not isinstance(ask_id, str):
        raise AssertionError(f"an ask recorded {done.get('status')}")
    return await _drive(env, lambda: ask_outcome(env.sq, env.ref.id, ask_id))


def _timeout(timeout_ms: int | None) -> int:
    """The deadline an ask or a wait takes. Only None means the default: a zero was refused at
    the boundary, so it never reaches here as "use the default"."""
    return TEAM_CONSTANTS.ask_wait_default_ms if timeout_ms is None else timeout_ms


async def _room(env: HandleEnv, to: MemberRef) -> bool:
    """Whether the asked member's budgets have room for one request of its model, read before the
    append as a model's ask reads it."""
    row = await env.sq.run(lambda c: member_named(c, env.ref.id, to.name))
    recipient = None if row is None else await recipient_of(env.sq)(row)
    return recipient is None or await ask_room(env.sq.budgets, recipient)


async def wait_for(
    env: HandleEnv,
    members: Sequence[MemberRef],
    mode: WaitMode | None,
    timeout_ms: int | None,
    key: str | None,
) -> TeamWaitResult:
    distinct = wait_members(members, mode)
    # Refused before any writer, so, like busy, it records nothing.
    if distinct == "invalid_request" or (timeout_ms is not None and not is_pos_int(timeout_ms)):
        return TeamWaitRefused("invalid_request")
    body: dict[str, JsonValue] = {
        "members": [to_json(m) for m in members],
        "mode": mode,
        "timeout_ms": timeout_ms,
    }
    # The wait's id is its request's mail id; a replay re-attaches with the first request's.
    opened: list[str] = []

    def decide(req: Request, team: TeamRow, close: CloseContext) -> dict[str, JsonValue]:
        opened.append(req.mail_id)
        targets = [ref_target(req.conn, team, m) for m in distinct]
        got = open_wait(req, close, targets, "all" if mode is None else mode, _timeout(timeout_ms))
        if not isinstance(got, dict):
            raise AssertionError("a wait records an object")
        return got

    done = await operator(env, "wait", body, key, decide)
    if done == BUSY:
        return TeamWaitRefused("busy")
    if done.get("status") == "refused":
        return TeamWaitRefused(code_in(_WAIT, done))
    waiting = done.get("wait_id")
    wait_id = waiting if isinstance(waiting, str) else opened[-1]
    return await _drive(env, lambda: wait_outcome(env.sq, env.ref.id, wait_id))


async def cancel_member(env: HandleEnv, member: MemberRef, key: str | None) -> TeamCancelResult:
    body: dict[str, JsonValue] = {"member": member.model_dump(mode="json")}
    done = await operator(
        env,
        "cancel",
        body,
        key,
        lambda req, team, _c: request_cancel(req, ref_target(req.conn, team, member)),
    )
    if done == BUSY:
        return TeamCancelRefused("busy")
    if done.get("status") == "cancel_requested":
        return CancelRequested(MemberRef.model_validate(done["member"]))
    return TeamCancelRefused(code_in(_CANCEL, done))


async def _drive[T: AskOutcome | Waited](
    env: HandleEnv, outcome: Callable[[], Awaitable[T | None]]
) -> T:
    """`outcome()` once it has one, driving the team meanwhile. Raises a member run's bug. It has
    no ceiling of its own: the outcome comes from the team log's deadline step, which only the
    lease holder runs, so a lease held elsewhere leaves this call waiting on that holder."""
    first = await outcome()
    if first is not None:
        return first
    worker = TeamWorker(env.worker)
    worker.start()
    try:
        while True:
            # Taken before the read, so progress made after it still wakes this loop.
            progress = asyncio.ensure_future(worker.progress())
            got = await outcome()
            if got is not None:
                progress.cancel()
                return got
            poll = TEAM_CONSTANTS.wake_poll_in_process_ms / 1000
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(progress, poll)
    finally:
        await worker.stop()


_OBSERVE: Final[tuple[ObserveRefusal, ...]] = ("forbidden", "unknown_member", "stale_member")
_ASK: Final[tuple[AskRefusal | OperatorRefusal, ...]] = (
    *_OBSERVE, "member_ended", "lead", "mailbox_full", "team_closed", "budget_exceeded", *KEYED,
)  # fmt: skip
_WAIT: Final[tuple[ObserveRefusal | OperatorRefusal, ...]] = (*_OBSERVE, *KEYED)
_CANCEL: Final[tuple[CancelRefusal | OperatorRefusal, ...]] = (*_OBSERVE, "member_ended", *KEYED)
"""The codes the team log can record for each op (its key refusals included)."""
