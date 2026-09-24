"""The model tools start and send (spec/schema/README.md, "Teams"; design §4.5 and §4.10), each
decided inside the caller's append: the policy decision, then the op's checks in the order the op
vectors pin, then its events and the call's one result. Reference:
spec/tools/fixtures/ops_member.py and ops_send.py."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import JsonValue

from threads.reduce.handlers import to_json
from threads.store.lines import Draft
from threads.team.call import (
    CallContext,
    Caller,
    Refusal,
    addressed,
    call_mail_id,
    caller_of,
    causal_of,
    decide,
    recorded,
)
from threads.team.dynamic import InvalidDefinition, Resolved
from threads.team.mail import body_of, sent
from threads.team.rows import MemberRow, member_rows, pending_to


@dataclass(frozen=True, slots=True)
class TeamLimits:
    """agent(team_limits=...): 4 running members and 100 pending mails per member by default."""

    concurrent: int = 4
    mailbox: int = 100


@dataclass(frozen=True, slots=True)
class StartPlan:
    """What start reads besides the store: the caller's definition."""

    agents: Mapping[str, str]
    """The agents the caller's team lists, by name, with the config_hash each pins as a member."""
    limits: TeamLimits
    headroom: Callable[[str], bool]
    """Every budget covering the new member has room for one request of its model."""
    thread_id: str
    """The new member's thread id, minted before the append."""
    resolved: Resolved | InvalidDefinition = field(default_factory=Resolved)
    """The start's label and chosen fields, resolved against the agent (resolve_definition)."""


_LIVE = frozenset({"starting", "running"})


def start(ctx: CallContext, agent: str, task: str, plan: StartPlan) -> JsonValue:
    """member.start: member_started and its task mail, which insert the starting row, the
    pending task and the starter's task monitor."""
    caller = caller_of(ctx)
    refused = _start_checks(ctx, caller, agent, plan)
    if refused is not None:
        return recorded(ctx, refused)
    team = caller.team.team_id
    k = 1 + sum(r.role == "member" and r.agent == agent for r in member_rows(ctx.conn, team))
    member: dict[str, JsonValue] = {
        "tenant": caller.team.tenant_id,
        "team": team,
        "name": f"{agent}-{k}",
        "generation": 1,
    }
    started_id = ctx.batch.next_id()
    parent: dict[str, JsonValue] = {
        "thread_id": ctx.call.thread_id,
        "branch_id": ctx.call.branch_id,
        "event_id": started_id,
        "relation": "team_member",
    }
    data: dict[str, JsonValue] = {
        "member": member,
        "agent": agent,
        "config_hash": plan.agents[agent],
        "thread_id": plan.thread_id,
        "parent": parent,
        "provenance": caller.provenance,
    }
    if isinstance(plan.resolved, Resolved) and plan.resolved.define is not None:
        data["define"] = to_json(plan.resolved.define)
    if isinstance(plan.resolved, Resolved) and plan.resolved.label is not None:
        data["label"] = plan.resolved.label
    ctx.batch.add(Draft("member_started", data))
    task_mail: dict[str, JsonValue] = {
        "mail_id": call_mail_id(ctx),
        "kind": "task",
        "team": team,
        "from": caller.ref,
        "to": {"name": member["name"], "generation": 1},
        "provenance": caller.provenance,
        "causal": causal_of(ctx),
        "body": {"text": task},
    }
    ctx.batch.add(sent(task_mail))
    return recorded(ctx, {"member": member, "status": "started"})


def _start_checks(ctx: CallContext, caller: Caller, agent: str, plan: StartPlan) -> Refusal | None:
    """Policy (only a lead starts), team open, the agent listed, its chosen fields, the
    concurrent cap, headroom."""
    denied = decide(ctx, "start", agent, allow=caller.row.role == "lead")
    if denied is not None:
        return denied
    if caller.team.closed_at is not None:
        return Refusal("team_closed")
    if agent not in plan.agents:
        return Refusal("unknown_agent")
    if isinstance(plan.resolved, InvalidDefinition):
        return Refusal("invalid_definition", plan.resolved)
    rows = member_rows(ctx.conn, caller.team.team_id)
    if sum(r.role == "member" and r.state in _LIVE for r in rows) >= plan.limits.concurrent:
        return Refusal("concurrency_cap")
    return None if plan.headroom(agent) else Refusal("budget_exceeded")


def send(ctx: CallContext, to: str, text: str, limits: TeamLimits) -> JsonValue:
    """mail.send: a message to a member, pending until its writer consumes it."""
    caller = caller_of(ctx)
    row = deliverable(ctx, caller, "send", to, limits)
    if isinstance(row, Refusal):
        return recorded(ctx, row)
    mail_id = call_mail_id(ctx)
    message: dict[str, JsonValue] = {
        "mail_id": mail_id,
        "kind": "message",
        "team": caller.team.team_id,
        "from": caller.ref,
        "to": {"name": row.name, "generation": row.generation},
        "provenance": caller.provenance,
        "causal": causal_of(ctx),
        "body": body_of(text, ctx.put),
    }
    ctx.batch.add(sent(message))
    return recorded(ctx, {"id": mail_id, "status": "sent"})


def deliverable(
    ctx: CallContext, caller: Caller, op: Literal["send", "ask"], to: str, limits: TeamLimits
) -> MemberRow | Refusal:
    """send and ask, after the policy: team open, the member known at its generation, not
    ended, not the sender, and its mailbox not full."""
    denied = decide(ctx, op, to, allow=True)
    if denied is not None:
        return denied
    if caller.team.closed_at is not None:
        return Refusal("team_closed")
    row = addressed(ctx, caller, to)
    if isinstance(row, Refusal):
        return row
    if row.state == "ended":
        return Refusal("member_ended")
    if row.name == caller.row.name:
        return Refusal("self")
    full = len(pending_to(ctx.conn, row.team_id, row.name)) >= limits.mailbox
    return Refusal("mailbox_full") if full else row
