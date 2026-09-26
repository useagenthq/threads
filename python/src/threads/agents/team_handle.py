"""The operator's handle (spec/api.json Team): each of start, send, ask, wait and cancel is one
operator request decided in one team-log append (design §4.4), by the same ops as the model's tools
(team_waits.py has ask, wait and cancel); members, events and ask_status are pure reads."""

from collections.abc import AsyncIterator, Sequence
from typing import Final

from pydantic import JsonValue

from threads.agents.start_pin import Chosen, start_pin
from threads.agents.store import now_ms
from threads.agents.team_budgets import ancestors_of
from threads.agents.team_feed import team_events
from threads.agents.team_handle_types import (
    AskStatus,
    OperatorRefusal,
    TeamAskResult,
    TeamCancelResult,
    TeamCursor,
    TeamItem,
    TeamMember,
    TeamRef,
    TeamSendRefused,
    TeamSendResult,
    TeamStartRefused,
    TeamStartResult,
    TeamWaitResult,
)
from threads.agents.team_log import BUSY
from threads.agents.team_operator import (
    KEYED,
    HandleEnv,
    code_in,
    operator,
    stored_text,
)
from threads.agents.team_outcomes import ask_status
from threads.agents.team_roster import roster
from threads.agents.team_tools import SendRefusal, Sent, Started, StartRefusal
from threads.agents.team_waits import ask_member, cancel_member, wait_for
from threads.log import BranchId, Budget, MemberRef, Parent, ThreadId, ThreadStartedEvent
from threads.loop.budget import start_room
from threads.reduce.handlers import to_json
from threads.result import Err
from threads.store.lines import uuid7
from threads.team.close import CloseContext
from threads.team.dynamic import InvalidDefinition
from threads.team.operator import ref_target
from threads.team.ops import StartPlan, send, start
from threads.team.request import Request
from threads.team.rows import TeamRow, member_rows
from threads.team.watch import WaitMode

__all__ = ["HandleEnv", "Team"]


class Team:
    """spec/api.json `Team`: your code's handle on a team, RunResult.team or open_team. Every
    request is recorded in the team log as the principal it acts for; only busy, returned before
    the log could be written, is not."""

    __slots__ = ("_env", "ref")

    def __init__(self, env: HandleEnv) -> None:
        self._env = env
        self.ref: TeamRef = env.ref
        """This team's tenant and id."""

    async def start(  # noqa: PLR0913 - the start's own fields, each optional
        self,
        agent: str,
        task: str,
        *,
        budget: Budget | None = None,
        label: str | None = None,
        instructions: str | None = None,
        tools: Sequence[str] | None = None,
        model: str | None = None,
        idempotency_key: str | None = None,
    ) -> TeamStartResult:
        """Starts a member from an agent the team lists, with task as its first input. budget is
        the member's own, capped by policy; omitted, the agent's own budget."""
        chosen = Chosen(label, instructions, tools, model)
        return await _start(self._env, agent, task, chosen, budget, idempotency_key)

    async def send(
        self, to: MemberRef, text: str, *, idempotency_key: str | None = None
    ) -> TeamSendResult:
        """Sends a member a message: its next input, which wakes it if idle."""
        return await _send(self._env, to, text, idempotency_key)

    async def ask(
        self,
        to: MemberRef,
        question: str,
        *,
        timeout_ms: int | None = None,
        idempotency_key: str | None = None,
    ) -> TeamAskResult:
        """Asks a member a question and waits for its reply, its end, or the deadline."""
        return await ask_member(self._env, to, question, timeout_ms, idempotency_key)

    async def wait(
        self,
        members: Sequence[MemberRef],
        *,
        mode: WaitMode | None = None,
        timeout_ms: int | None = None,
        idempotency_key: str | None = None,
    ) -> TeamWaitResult:
        """Waits until members settle (become idle or end), or the deadline. mode: all (the
        default), any, or how many must settle; any never cancels the others."""
        return await wait_for(self._env, members, mode, timeout_ms, idempotency_key)

    async def cancel(
        self, member: MemberRef, *, idempotency_key: str | None = None
    ) -> TeamCancelResult:
        """Requests a member's cancel: durable at once, applied at the member's next step."""
        return await cancel_member(self._env, member, idempotency_key)

    async def ask_status(self, ask_id: str) -> AskStatus:
        """An ask's state from the team log. A pure read; it never closes an ask."""
        return await ask_status(self._env.sq, self.ref.id, ask_id)

    async def members(self) -> tuple[TeamMember, ...]:
        """Every member, the lead included, with its state and, once settled, its result."""
        return await roster(self._env.sq, self.ref.id)

    def events(self, *, after: TeamCursor | None = None) -> AsyncIterator[TeamItem]:
        """The team's audit feed: every committed event of every team log, as stored."""
        return team_events(self._env.sq, self.ref.id, after)


async def _start(  # noqa: PLR0913, PLR0917 - the start's own fields, each optional
    env: HandleEnv,
    agent: str,
    task: str,
    chosen: Chosen,
    budget: Budget | None,
    key: str | None,
) -> TeamStartResult:
    parent = await _lead_parent(env)
    got = await start_pin(env.pin, env.sq.put_artifact, agent, chosen, "operator")
    pinned = got.pinned
    room = pinned is not None and await start_room(
        env.sq.budgets, await ancestors_of(env.sq, parent), pinned, budget
    )
    listed = {} if pinned is None else {agent: pinned.config_hash}
    plan = StartPlan(listed, env.limits, lambda _a: room, uuid7(now_ms()), got.resolved, budget)
    body: dict[str, JsonValue] = {"agent": agent, "task": task, **_fields(chosen)}
    if budget is not None:
        body["budget"] = to_json(budget)
    done = await operator(
        env, "start", body, key, lambda req, _t, _c: start(req, agent, task, plan)
    )
    if done == BUSY:
        return TeamStartRefused("busy")
    if done.get("status") == "started":
        member = MemberRef.model_validate(done["member"])
        return Started(member)
    return TeamStartRefused(code_in(_START, done), detail=_detail(done.get("detail")))


async def _send(env: HandleEnv, to: MemberRef, text: str, key: str | None) -> TeamSendResult:
    big = await stored_text(env, text)
    body: dict[str, JsonValue] = {"to": to.model_dump(mode="json"), "text": text}

    def decide(req: Request, team: TeamRow, _close: CloseContext) -> dict[str, JsonValue]:
        return send(req, ref_target(req.conn, team, to), text, env.limits)

    done = await operator(env, "send", body, key, decide, big=big)
    if done == BUSY:
        return TeamSendRefused("busy")
    if done.get("status") == "sent":
        return Sent(str(done["id"]))
    return TeamSendRefused(code_in(_SEND, done))


async def _lead_parent(env: HandleEnv) -> Parent:
    """The lead as a new member's parent: its budgets and its ancestors' cover the member."""
    rows = await env.sq.run(lambda c: member_rows(c, env.ref.id))
    lead = next((r for r in rows if r.role == "lead"), None)
    if lead is None or lead.branch_id is None:
        raise AssertionError(f"team {env.ref.id} has no lead log")
    read = await env.sq.read(BranchId(lead.branch_id), now_ms())
    if isinstance(read, Err):
        raise AssertionError(f"team {env.ref.id}: {read.error.message}")
    started = next(e for e in read.value.fold.events if isinstance(e, ThreadStartedEvent))
    return Parent(
        thread_id=ThreadId(lead.thread_id),
        branch_id=BranchId(lead.branch_id),
        event_id=started.event_id,
        relation="team_member",
    )


def _fields(chosen: Chosen) -> dict[str, JsonValue]:
    tools: JsonValue = None if chosen.tools is None else list[JsonValue](chosen.tools)
    return {
        "label": chosen.label,
        "instructions": chosen.instructions,
        "tools": tools,
        "model": chosen.model,
    }


type StartCode = StartRefusal | OperatorRefusal
type SendCode = SendRefusal | OperatorRefusal
_START: Final[tuple[StartCode, ...]] = (
    "forbidden", "unknown_agent", "concurrency_cap", "budget_exceeded", "team_closed",
    "invalid_definition", *KEYED,
)  # fmt: skip
_SEND: Final[tuple[SendCode, ...]] = (
    "forbidden", "unknown_member", "stale_member", "member_ended", "self", "mailbox_full",
    "team_closed", *KEYED,
)  # fmt: skip
"""The codes the team log can record for each op (its key refusals included)."""


def _detail(raw: JsonValue) -> InvalidDefinition | None:
    if not isinstance(raw, dict):
        return None
    allowed = raw.get("allowed")
    field_, reason = raw.get("field"), raw.get("reason")
    fields = ("label", "instructions", "tools", "model")
    if field_ not in fields or reason not in ("not_allowed", "invalid"):
        raise AssertionError("an invalid_definition detail names its field and reason")
    return InvalidDefinition(
        next(f for f in fields if f == field_),
        "not_allowed" if reason == "not_allowed" else "invalid",
        None if not isinstance(allowed, list) else tuple(str(a) for a in allowed),
    )
