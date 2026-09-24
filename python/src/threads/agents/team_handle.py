"""The operator's handle (spec/api.json Team): each of start and send is one operator request
decided in one team-log append (design §4.4), by the same ops as the model's tools; members and
events are pure reads. Lane 21E adds ask, wait, cancel and ask_status as further requests."""

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from pydantic import JsonValue

from threads.agents.start_pin import Chosen, start_pin
from threads.agents.store import now_ms
from threads.agents.team_budgets import ancestors_of
from threads.agents.team_feed import team_events
from threads.agents.team_handle_types import (
    OperatorRefusal,
    TeamCursor,
    TeamItem,
    TeamMember,
    TeamRef,
    TeamSendRefused,
    TeamSendResult,
    TeamStartRefused,
    TeamStartResult,
)
from threads.agents.team_log import BUSY, on_team_log
from threads.agents.team_roster import roster
from threads.agents.team_tools import SendRefusal, Sent, Started, StartRefusal
from threads.agents.teams import Pin
from threads.log import BranchId, MemberRef, Parent, Principal, ThreadId, ThreadStartedEvent
from threads.loop.budget import start_room
from threads.result import Err
from threads.store import SqliteStore
from threads.store.lines import uuid7
from threads.store.writer import DecideTx
from threads.team.batch import Batch, Mint
from threads.team.constants import TEAM_CONSTANTS
from threads.team.dynamic import InvalidDefinition, Resolved
from threads.team.operator import (
    OperatorContext,
    OperatorInput,
    OperatorOp,
    Replayed,
    open_operator,
    ref_target,
)
from threads.team.ops import StartPlan, TeamLimits, send, start
from threads.team.request import Request
from threads.team.rows import TeamRow, member_rows, team_row


@dataclass(frozen=True, slots=True)
class HandleEnv:
    sq: SqliteStore
    ref: TeamRef
    principal: Principal
    pin: Pin | None
    """The lead's team as this process defines it: the agents start resolves. None: none here."""
    limits: TeamLimits = field(default_factory=TeamLimits)
    busy_bound_ms: int | None = None
    mint: Mint | None = None


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
        label: str | None = None,
        instructions: str | None = None,
        tools: Sequence[str] | None = None,
        model: str | None = None,
        idempotency_key: str | None = None,
    ) -> TeamStartResult:
        """Starts a member from an agent the team lists, with task as its first input."""
        chosen = Chosen(label, instructions, tools, model)
        return await _start(self._env, agent, task, chosen, idempotency_key)

    async def send(
        self, to: MemberRef, text: str, *, idempotency_key: str | None = None
    ) -> TeamSendResult:
        """Sends a member a message: its next input, which wakes it if idle."""
        return await _send(self._env, to, text, idempotency_key)

    async def members(self) -> tuple[TeamMember, ...]:
        """Every member, the lead included, with its state and, once settled, its result."""
        return await roster(self._env.sq, self.ref.id)

    def events(self, *, after: TeamCursor | None = None) -> AsyncIterator[TeamItem]:
        """The team's audit feed: every committed event of every team log, as stored."""
        return team_events(self._env.sq, self.ref.id, after)


async def _start(
    env: HandleEnv, agent: str, task: str, chosen: Chosen, key: str | None
) -> TeamStartResult:
    parent = await _lead_parent(env)
    got = (
        None
        if env.pin is None
        else await start_pin(env.pin, env.sq.put_artifact, agent, chosen, "operator")
    )
    pinned = None if got is None else got.pinned
    room = pinned is not None and await start_room(
        env.sq.budgets, await ancestors_of(env.sq, parent), pinned
    )
    listed = {} if pinned is None else {agent: pinned.config_hash}
    resolved = Resolved() if got is None else got.resolved
    plan = StartPlan(listed, env.limits, lambda _a: room, uuid7(now_ms()), resolved)
    body = _present({"agent": agent, "task": task, **_fields(chosen)})
    done = await _operator(env, "start", body, key, lambda req, _t: start(req, agent, task, plan))
    if done == BUSY:
        return TeamStartRefused("busy")
    if done.get("status") == "started":
        member = MemberRef.model_validate(done["member"])
        return Started(member)
    return TeamStartRefused(_code(_START, done), detail=_detail(done.get("detail")))


async def _send(env: HandleEnv, to: MemberRef, text: str, key: str | None) -> TeamSendResult:
    big: JsonValue = None
    if len(text.encode()) > TEAM_CONSTANTS.inline_cap_bytes:
        data = text.encode()
        sha = await env.sq.put_artifact(data)
        big = {"sha256": sha, "bytes": len(data), "media_type": "text/plain"}
    body: dict[str, JsonValue] = {"to": to.model_dump(mode="json"), "text": text}

    def decide(req: Request, team: TeamRow) -> dict[str, JsonValue]:
        return send(req, ref_target(req.conn, team, to), text, env.limits)

    done = await _operator(env, "send", body, key, decide, big=big)
    if done == BUSY:
        return TeamSendRefused("busy")
    if done.get("status") == "sent":
        return Sent(str(done["id"]))
    return TeamSendRefused(_code(_SEND, done))


async def _operator(  # noqa: PLR0913 - the request and what it decides
    env: HandleEnv,
    op: OperatorOp,
    body: Mapping[str, JsonValue],
    key: str | None,
    decide: Callable[[Request, TeamRow], dict[str, JsonValue]],
    *,
    big: JsonValue = None,
) -> dict[str, JsonValue] | Literal["busy"]:
    """One operator request under the team-log writer: its key looked up, then `decide` with
    the op. Returns what the op or the key's replay recorded, or busy."""
    team = await env.sq.run(lambda c: team_row(c, env.ref.id))
    if team is None:
        raise AssertionError(f"no team {env.ref.id}")
    request = OperatorInput(uuid7(now_ms()), op, env.principal, body, key)

    def run(tx: DecideTx, batch: Batch) -> dict[str, JsonValue]:
        # Read in the append: the team may have closed since the handle looked.
        now = team_row(tx.conn, env.ref.id) or team
        ctx = OperatorContext(tx.conn, tx.fold.events, batch, lambda _text: big, now)
        opened = open_operator(ctx, request)
        return opened.outcome if isinstance(opened, Replayed) else decide(opened.request, now)

    return await on_team_log(
        env.sq,
        BranchId(team.team_log_branch_id),
        run,
        busy_bound_ms=env.busy_bound_ms,
        mint=env.mint,
    )


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


def _present(body: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """A body's parameters as api.json names them, an omitted option left out."""
    return {k: v for k, v in body.items() if v is not None}


type StartCode = StartRefusal | OperatorRefusal
type SendCode = SendRefusal | OperatorRefusal
_KEYED: Final = ("idempotency_key_reused", "idempotency_key_principal_mismatch")
_START: Final[tuple[StartCode, ...]] = (
    "forbidden", "unknown_agent", "concurrency_cap", "budget_exceeded", "team_closed",
    "invalid_definition", *_KEYED,
)  # fmt: skip
_SEND: Final[tuple[SendCode, ...]] = (
    "forbidden", "unknown_member", "stale_member", "member_ended", "self", "mailbox_full",
    "team_closed", *_KEYED,
)  # fmt: skip
"""The codes the team log can record for each op (its key refusals included)."""


def _code[C: str](codes: tuple[C, ...], done: Mapping[str, JsonValue]) -> C:
    code = done.get("code")
    for known in codes:
        if code == known:
            return known
    raise AssertionError(f"the team log recorded {code}")


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
