"""One team tool call under the caller's writer (spec/schema/README.md, "Teams", Model tools): its
policy decision, its refusal and its success are recorded the same way for every op, and the
call's one tool_result carries the op's result as RFC 8785 JSON. Reference:
spec/tools/fixtures/ops_request.py."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import JsonValue

from threads.log import (
    Event,
    MemberRef,
    MemberStartedEvent,
    MessageReceivedEvent,
    TeamRefusal,
    ToolCallEvent,
)
from threads.log.jcs import canonicalize
from threads.result import Ok
from threads.store.lines import Draft
from threads.team.batch import Batch
from threads.team.mail import PutText
from threads.team.provenance import turn_provenance
from threads.team.rows import MemberRow, TeamRow, member_named, own_rows, ref_of, team_row


@dataclass(frozen=True, slots=True)
class CallContext:
    """What an op's decision reads, inside the caller's append transaction."""

    conn: sqlite3.Connection
    events: Sequence[Event]
    """The caller's committed events: its turn, the call and what its log recorded."""
    batch: Batch
    call: ToolCallEvent
    put: PutText


@dataclass(frozen=True, slots=True)
class Caller:
    """The caller as one team's member: its row there, its ref, its turn's provenance."""

    team: TeamRow
    row: MemberRow
    ref: dict[str, JsonValue]
    provenance: JsonValue


@dataclass(frozen=True, slots=True)
class Refusal:
    """An op's refusal: the op records it as the call's result."""

    code: TeamRefusal


def caller_of(ctx: CallContext) -> Caller:
    """The team the caller acts in: the one it leads (a nested lead starts its own members),
    else the one it is a member of."""
    rows = own_rows(ctx.conn, ctx.call.thread_id)
    row = next((r for r in rows if r.role == "lead"), rows[0] if rows else None)
    team = None if row is None else team_row(ctx.conn, row.team_id)
    provenance = turn_provenance(ctx.conn, ctx.events)
    if row is None or team is None or provenance is None:
        raise AssertionError("a team tool call outside a team")
    return Caller(team, row, ref_of(team, row), provenance)


def call_mail_id(ctx: CallContext) -> str:
    """The call's mail: `<sender branch_id>:<call_id>`."""
    return f"{ctx.call.branch_id}:{ctx.call.data.call_id}"


def causal_of(ctx: CallContext) -> JsonValue:
    """The request that caused the call's mail: its tool_call."""
    return {"thread_id": ctx.call.thread_id, "event_id": ctx.call.event_id}


def decide(ctx: CallContext, op: str, target: str, *, allow: bool) -> Refusal | None:
    """Phase 1 policy: the team's grant (source team), else default deny, which refuses
    forbidden. The decision is recorded either way."""
    data: dict[str, JsonValue] = {
        "op": op,
        "decision": "allow" if allow else "deny",
        "source": "team" if allow else "default",
        "target": target,
        "call_id": ctx.call.data.call_id,
    }
    ctx.batch.add(Draft("message_policy_decided", data))
    return None if allow else Refusal("forbidden")


def answer(ctx: CallContext, value: JsonValue) -> Draft:
    """The call's one tool_result: its value as RFC 8785 JSON."""
    preview = canonicalize(value)
    if not isinstance(preview, Ok):
        raise AssertionError("a team op's result is JSON")
    data: dict[str, JsonValue] = {
        "call_id": ctx.call.data.call_id,
        "is_error": False,
        "completeness": "complete",
        "preview": preview.value,
        "origin": "executed",
    }
    return Draft("tool_result", data, {"kind": "tool"})


def recorded(ctx: CallContext, value: JsonValue | Refusal) -> None:
    """Records the op's result, or its refusal, as the call's result."""
    if isinstance(value, Refusal):
        value = {"code": value.code, "status": "refused"}
    ctx.batch.add(answer(ctx, value))


def addressed(ctx: CallContext, caller: Caller, name: str) -> MemberRow | Refusal:
    """The member a model addresses by name, at the generation the caller's own log last
    recorded for it (its member_started, or a receipt's sender), else the current row's."""
    row = member_named(ctx.conn, caller.team.team_id, name)
    seen = _bound(ctx.events, name)
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
