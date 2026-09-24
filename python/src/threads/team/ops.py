"""start and send (spec/schema/README.md, "Teams"; design §4.5 and §4.10) for a model call or an
operator request, each decided inside the request's append: the policy decision, then the op's
checks in the order the op vectors pin, then its events and the recorded outcome. Reference:
spec/tools/fixtures/ops_member.py and ops_send.py."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import JsonValue

from threads.reduce.handlers import to_json
from threads.store.lines import Draft
from threads.team.call import Refusal
from threads.team.dynamic import InvalidDefinition, Resolved
from threads.team.mail import body_of, sent
from threads.team.request import Request, Target
from threads.team.rows import MemberRow, member_rows, pending_to


@dataclass(frozen=True, slots=True)
class TeamLimits:
    """agent(team_limits=...): 4 running members and 100 pending mails per member by default."""

    concurrent: int = 4
    mailbox: int = 100


@dataclass(frozen=True, slots=True)
class StartPlan:
    """What start reads besides the store: the starter's definition."""

    agents: Mapping[str, str]
    """The agents the team lists, by name, with the config_hash each pins as a member."""
    limits: TeamLimits
    headroom: Callable[[str], bool]
    """Every budget covering the new member has room for one request of its model."""
    thread_id: str
    """The new member's thread id, minted before the append."""
    resolved: Resolved | InvalidDefinition = field(default_factory=Resolved)
    """The start's label and chosen fields, resolved against the agent (resolve_definition)."""


_LIVE = frozenset({"starting", "running"})


def start(req: Request, agent: str, task: str, plan: StartPlan) -> dict[str, JsonValue]:
    """member.start: member_started and its task mail, which insert the starting row, the
    pending task and the starter's task monitor."""
    refused = _start_checks(req, agent, plan)
    if refused is not None:
        return req.refuse(refused)
    team = req.team.team_id
    k = 1 + sum(r.role == "member" and r.agent == agent for r in member_rows(req.conn, team))
    member: dict[str, JsonValue] = {
        "tenant": req.team.tenant_id,
        "team": team,
        "name": f"{agent}-{k}",
        "generation": 1,
    }
    started_id = req.batch.next_id()
    data: dict[str, JsonValue] = {
        "member": member,
        "agent": agent,
        "config_hash": plan.agents[agent],
        "thread_id": plan.thread_id,
        "parent": req.parent(started_id),
        "provenance": req.provenance,
    }
    if isinstance(plan.resolved, Resolved) and plan.resolved.define is not None:
        data["define"] = to_json(plan.resolved.define)
    if isinstance(plan.resolved, Resolved) and plan.resolved.label is not None:
        data["label"] = plan.resolved.label
    req.batch.add(Draft("member_started", data))
    task_mail: dict[str, JsonValue] = {
        "mail_id": req.mail_id,
        "kind": "task",
        "team": team,
        "from": req.sender,
        "to": {"name": member["name"], "generation": 1},
        "provenance": req.provenance,
        "causal": req.causal,
        "body": {"text": task},
    }
    req.batch.add(sent(task_mail))
    return req.done({"member": member, "status": "started"})


def _start_checks(req: Request, agent: str, plan: StartPlan) -> Refusal | None:
    """Policy (a lead, or the operator of the team's tenant, starts), team open, the agent
    listed, its chosen fields, the concurrent cap, headroom."""
    denied = req.decide("start", agent)
    if denied is not None:
        return denied
    if req.team.closed_at is not None:
        return Refusal("team_closed")
    if agent not in plan.agents:
        return Refusal("unknown_agent")
    if isinstance(plan.resolved, InvalidDefinition):
        return Refusal("invalid_definition", plan.resolved)
    rows = member_rows(req.conn, req.team.team_id)
    if sum(r.role == "member" and r.state in _LIVE for r in rows) >= plan.limits.concurrent:
        return Refusal("concurrency_cap")
    return None if plan.headroom(agent) else Refusal("budget_exceeded")


def send(req: Request, to: Target, text: str, limits: TeamLimits) -> dict[str, JsonValue]:
    """mail.send: a message to a member, pending until its writer consumes it."""
    row = deliverable(req, "send", to, limits)
    if isinstance(row, Refusal):
        return req.refuse(row)
    message: dict[str, JsonValue] = {
        "mail_id": req.mail_id,
        "kind": "message",
        "team": req.team.team_id,
        "from": req.sender,
        "to": {"name": row.name, "generation": row.generation},
        "provenance": req.provenance,
        "causal": req.causal,
        "body": body_of(text, req.put),
    }
    req.batch.add(sent(message))
    return req.done({"id": req.mail_id, "status": "sent"})


def deliverable(
    req: Request, op: Literal["send", "ask"], to: Target, limits: TeamLimits
) -> MemberRow | Refusal:
    """send and ask, after the policy: team open, the member known at its generation, not
    ended, not the sender, and its mailbox not full."""
    denied = req.decide(op, to.name)
    if denied is not None:
        return denied
    if req.team.closed_at is not None:
        return Refusal("team_closed")
    row = to.row()
    if isinstance(row, Refusal):
        return row
    if row.state == "ended":
        return Refusal("member_ended")
    if req.own is not None and row.name == req.own.name:
        return Refusal("self")
    full = len(pending_to(req.conn, row.team_id, row.name)) >= limits.mailbox
    return Refusal("mailbox_full") if full else row
