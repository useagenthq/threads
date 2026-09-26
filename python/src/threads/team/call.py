"""One team tool call under the caller's writer (spec/schema/README.md, "Teams", Model tools): its
policy decision, its refusal and its success are recorded the same way for every op, and the
call's one tool_result carries the op's result as RFC 8785 JSON. Reference:
spec/tools/fixtures/ops_request.py."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from threads.log import (
    ArtifactRef,
    Event,
    MemberRef,
    MemberStartedEvent,
    MessageReceivedEvent,
    TeamRefusal,
    ToolCallEvent,
)
from threads.log.jcs import canonicalize
from threads.reduce import Fold
from threads.result import Ok
from threads.store.conn import Conn
from threads.store.lines import Draft
from threads.team.batch import Batch
from threads.team.dynamic import InvalidDefinition
from threads.team.mail import PutText
from threads.team.policy import MessagePolicyRule, rule_for
from threads.team.provenance import turn_provenance
from threads.team.request import PolicyOp, Request, Target
from threads.team.rows import MemberRow, TeamRow, member_named, own_rows, ref_of, team_row

type ReadText = Callable[[ArtifactRef], str]
"""Reads a {ref} body's text from the content-addressed store (sha256 and length verified)."""


@dataclass(frozen=True, slots=True)
class CallContext:
    """What an op's decision reads, inside the caller's append transaction."""

    conn: Conn
    fold: Fold
    """The caller's committed fold: its turn, the call and what its log recorded."""
    batch: Batch
    call: ToolCallEvent
    put: PutText
    read: ReadText
    rules: tuple[MessagePolicyRule, ...] = ()
    """The host's message_policy rules with the calling agent as `from`; none outside a host."""


@dataclass(frozen=True, slots=True)
class Caller:
    """The caller as one team's member: its row there, its ref, its turn's provenance."""

    team: TeamRow
    row: MemberRow
    ref: dict[str, JsonValue]
    provenance: JsonValue


type CallRefusal = TeamRefusal | Literal["unknown_ask", "already_replied", "ask_closed"]
"""Why a call was refused: a team refusal, or one of reply's own."""


@dataclass(frozen=True, slots=True)
class Refusal:
    """An op's refusal: the op records it as the call's result; a start's fields also say why."""

    code: CallRefusal
    detail: InvalidDefinition | None = None


def caller_of(ctx: CallContext) -> Caller:
    """The team the caller acts in: the one it leads (a nested lead starts its own members),
    else the one it is a member of."""
    rows = own_rows(ctx.conn, ctx.call.thread_id)
    row = next((r for r in rows if r.role == "lead"), rows[0] if rows else None)
    team = None if row is None else team_row(ctx.conn, row.team_id)
    provenance = turn_provenance(ctx.conn, ctx.fold.events)
    if row is None or team is None or provenance is None:
        raise AssertionError("a team tool call outside a team")
    return Caller(team, row, ref_of(team, row), provenance)


def call_mail_id(ctx: CallContext) -> str:
    """The call's mail: `<sender branch_id>:<call_id>`."""
    return f"{ctx.call.branch_id}:{ctx.call.data.call_id}"


def causal_of(ctx: CallContext) -> JsonValue:
    """The request that caused the call's mail: its tool_call."""
    return {"thread_id": ctx.call.thread_id, "event_id": ctx.call.event_id}


@dataclass(frozen=True, slots=True)
class Decision:
    """What decided an op: the team's grant, a host rule that adds to it, or default deny."""

    source: Literal["team", "message_policy", "default"]
    rule: MessagePolicyRule | None = None
    """message_policy only: the rule, named by its from and to."""


def decision(ctx: CallContext, caller: Caller, op: PolicyOp, target: str) -> Decision:
    """The decision order (spec/api.json host.message_policy): the team's grant, then the host's
    rules, which only add, then default deny."""
    if _granted(ctx, caller, op, target):
        return Decision("team")
    rule = rule_for(ctx.rules, caller.row.agent, target, op)
    return Decision("default") if rule is None else Decision("message_policy", rule)


def decide(
    ctx: CallContext,
    op: PolicyOp,
    target: str,
    decided: Decision,
) -> Refusal | None:
    """Records the decision; default deny refuses forbidden."""
    allow = decided.source != "default"
    data: dict[str, JsonValue] = {
        "op": op,
        "decision": "allow" if allow else "deny",
        "source": decided.source,
        "target": target,
        "call_id": ctx.call.data.call_id,
    }
    if decided.rule is not None:
        data["rule"] = {"from": decided.rule["from"], "to": decided.rule["to"]}
    ctx.batch.add(Draft("message_policy_decided", data))
    return None if allow else Refusal("forbidden")


def answer(ctx: CallContext, value: JsonValue) -> Draft:
    """The call's one tool_result: its value as RFC 8785 JSON."""
    return tool_result(ctx.call.data.call_id, value)


def tool_result(call_id: str, value: JsonValue) -> Draft:
    """A team call's one tool_result, by call id: its value as RFC 8785 JSON."""
    preview = canonicalize(value)
    if not isinstance(preview, Ok):
        raise AssertionError("a team op's result is JSON")
    data: dict[str, JsonValue] = {
        "call_id": call_id,
        "is_error": False,
        "completeness": "complete",
        "preview": preview.value,
        "origin": "executed",
    }
    return Draft("tool_result", data, {"kind": "tool"})


def refused_of(refusal: Refusal) -> dict[str, JsonValue]:
    """A refusal's recorded value."""
    value: dict[str, JsonValue] = {"code": refusal.code, "status": "refused"}
    if refusal.detail is not None:
        value["detail"] = refusal.detail.to_json()
    return value


def recorded(ctx: CallContext, value: JsonValue | Refusal) -> JsonValue:
    """Records the op's result, or its refusal, as the call's result; returns what it recorded."""
    out = refused_of(value) if isinstance(value, Refusal) else value
    ctx.batch.add(answer(ctx, out))
    return out


def _done(ctx: CallContext, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    ctx.batch.add(answer(ctx, value))
    return value


def call_request(ctx: CallContext) -> Request:
    """The call as a team request: its caller sends, and its tool_result records the outcome."""
    caller = caller_of(ctx)

    def decide_(op: PolicyOp, target: str) -> Refusal | None:
        return decide(ctx, op, target, decision(ctx, caller, op, target))

    def parent(started_id: str) -> JsonValue:
        return {
            "thread_id": ctx.call.thread_id,
            "branch_id": ctx.call.branch_id,
            "event_id": started_id,
            "relation": "team_member",
        }

    return Request(
        ctx.conn,
        ctx.batch,
        ctx.put,
        caller.team,
        caller.ref,
        caller.provenance,
        causal_of(ctx),
        call_mail_id(ctx),
        caller.row,
        decide_,
        parent,
        lambda refusal: _done(ctx, refused_of(refusal)),
        lambda value: _done(ctx, value),
    )


def _granted(ctx: CallContext, caller: Caller, op: PolicyOp, target: str) -> bool:
    """The team's Phase 1 grant to a member: a lead starts, a member's starter (its member_started
    is in the caller's own log) cancels it, and members send, ask and monitor one another."""
    if op == "start":
        return caller.row.role == "lead"
    if op != "cancel":
        return True
    return any(
        isinstance(e, MemberStartedEvent) and e.data.member.name == target for e in ctx.fold.events
    )


def named(ctx: CallContext, name: str) -> Target:
    """A model's target: the member it names, at the generation its own log last recorded."""
    return Target(name, lambda: addressed(ctx, caller_of(ctx), name))


def addressed(ctx: CallContext, caller: Caller, name: str) -> MemberRow | Refusal:
    """The member a model addresses by name, at the generation the caller's own log last
    recorded for it (its member_started, or a receipt's sender), else the current row's."""
    row = member_named(ctx.conn, caller.team.team_id, name)
    seen = _bound(ctx.fold.events, name)
    generation = seen if seen is not None else (0 if row is None else row.generation)
    if row is None or generation > row.generation:
        return Refusal("unknown_member")
    return Refusal("stale_member") if generation < row.generation else row


def _bound(events: Sequence[Event], name: str) -> int | None:
    seen: int | None = None
    for e in events:
        if isinstance(e, MemberStartedEvent) and e.data.member.name == name:
            seen = e.data.member.generation
        elif isinstance(e, MessageReceivedEvent):
            sender = e.data.envelope.from_
            if isinstance(sender, MemberRef) and sender.name == name:
                seen = sender.generation
    return seen
