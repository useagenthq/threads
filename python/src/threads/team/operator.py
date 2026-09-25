"""An operator request in the team log (design §4.4; spec/schema/README.md, "Teams"): its key is
looked up first, then operator_request opens it, and it is decided by the same ops as a model call.
Reference: spec/tools/fixtures/ops_request.py (open_request, keyed, recorded)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    Event,
    MailEnvelope,
    MemberRef,
    MemberStartedEvent,
    MessageSentEvent,
    OperatorRefusedEvent,
    OperatorRequestEvent,
    OperatorSender,
    Principal,
    WaitStartedEvent,
)
from threads.log.digest import sha256_hex
from threads.log.jcs import canonicalize
from threads.log.keys import principal_key
from threads.reduce.handlers import to_json
from threads.result import Ok
from threads.store.conn import Conn
from threads.store.lines import Draft
from threads.store.sql import text_of
from threads.team.batch import Batch
from threads.team.call import Refusal, refused_of
from threads.team.mail import PutText
from threads.team.request import PolicyOp, Request, Target
from threads.team.rows import MemberRow, TeamRow, member_named, member_rows

type OperatorOp = Literal["start", "send", "ask", "wait", "cancel"]


@dataclass(frozen=True, slots=True)
class OperatorInput:
    request_id: str
    op: OperatorOp
    principal: Principal
    body: Mapping[str, JsonValue]
    """The method's parameters as spec/api.json names them, without idempotency_key and with every
    omitted option left out: body_hash is its RFC 8785 sha256."""
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class OperatorContext:
    """What an operator request reads, inside the team log's append."""

    conn: Conn
    events: Sequence[Event]
    """The team log's committed events."""
    batch: Batch
    put: PutText
    team: TeamRow


@dataclass(frozen=True, slots=True)
class Opened:
    """Decide it with the op."""

    request: Request


@dataclass(frozen=True, slots=True)
class Replayed:
    """Its key replays an earlier request's recorded outcome, or refuses the request."""

    outcome: dict[str, JsonValue]


def _body_hash(body: Mapping[str, JsonValue]) -> str:
    text = canonicalize(dict(body))
    if not isinstance(text, Ok):
        raise AssertionError("an operator request's body is JSON")
    return sha256_hex(text.value.encode())


def open_operator(ctx: OperatorContext, inp: OperatorInput) -> Opened | Replayed:
    """The key's binding, looked up before anything is recorded: the same principal and body
    replay the bound request's outcome and append nothing; another principal, then another body,
    is refused, recorded as a request without the key (the key stays bound to the first)."""
    key = inp.idempotency_key
    if key is None:
        return Opened(_opened(ctx, inp))
    row = ctx.conn.execute(
        "SELECT principal_key, body_hash, request_id FROM operator_receipts"
        " WHERE tenant_id = ? AND team_id = ? AND op = ? AND idempotency_key = ?",
        (ctx.team.tenant_id, ctx.team.team_id, inp.op, key),
    ).fetchone()
    if row is None:
        return Opened(_opened(ctx, inp))
    who, body_hash, rid = (text_of(v) for v in row)
    if who != principal_key(inp.principal):
        code = "idempotency_key_principal_mismatch"
    elif body_hash != _body_hash(inp.body):
        code = "idempotency_key_reused"
    else:
        return Replayed(_recorded(ctx.events, ctx.team, rid))
    unkeyed = replace(inp, idempotency_key=None)
    return Replayed(_opened(ctx, unkeyed).refuse(Refusal(code)))


def _opened(ctx: OperatorContext, inp: OperatorInput) -> Request:
    """operator_request, whose own event is the provenance's root request, then the request."""
    first = ctx.events[0]
    rid = inp.request_id
    principal = to_json(inp.principal)
    root: JsonValue = {"thread_id": first.thread_id, "event_id": ctx.batch.next_id()}
    provenance: JsonValue = {"principal": principal, "root_request": root, "via": []}
    data: dict[str, JsonValue] = {
        "request_id": rid,
        "op": inp.op,
        "principal": principal,
        "body_hash": _body_hash(inp.body),
        "provenance": provenance,
    }
    if inp.idempotency_key is not None:
        data["idempotency_key"] = inp.idempotency_key
    ctx.batch.add(Draft("operator_request", data, {"kind": "host", "principal": principal}))
    # An operator acting for a principal of another tenant is denied (Phase 1 policy).
    allow = inp.principal.tenant == ctx.team.tenant_id

    def decide(op: PolicyOp, target: str) -> Refusal | None:
        decision: dict[str, JsonValue] = {
            "op": op,
            "decision": "allow" if allow else "deny",
            "source": "team" if allow else "default",
            "target": target,
            "request_id": rid,
        }
        ctx.batch.add(Draft("message_policy_decided", decision))
        return None if allow else Refusal("forbidden")

    def refuse(refusal: Refusal) -> dict[str, JsonValue]:
        refused: dict[str, JsonValue] = {"request_id": rid, "code": refusal.code}
        if refusal.detail is not None:
            refused["detail"] = refusal.detail.to_json()
        ctx.batch.add(Draft("operator_refused", refused))
        return refused_of(refusal)

    return Request(
        ctx.conn,
        ctx.batch,
        ctx.put,
        ctx.team,
        {"operator": rid},
        provenance,
        root,
        f"{first.branch_id}:{rid}",
        None,
        decide,
        lambda _started: _lead_parent(ctx.conn, ctx.team),
        refuse,
        lambda value: value,
    )


def _lead_parent(conn: Conn, team: TeamRow) -> JsonValue:
    """An operator start's parent is the lead's thread_started, which carries the team."""
    lead = next((r for r in member_rows(conn, team.team_id) if r.role == "lead"), None)
    first = (
        None
        if lead is None
        else conn.execute(
            "SELECT event_id FROM events WHERE branch_id = ? AND seq = 1", (lead.branch_id,)
        ).fetchone()
    )
    if lead is None or lead.branch_id is None or first is None:
        raise AssertionError(f"team {team.team_id} has no lead log")
    return {
        "thread_id": lead.thread_id,
        "branch_id": lead.branch_id,
        "event_id": text_of(first[0]),
        "relation": "team_member",
    }


def ref_target(conn: Conn, team: TeamRow, ref: MemberRef) -> Target:
    """An operator's target: the member a ref names, known in this team at its generation."""

    def row() -> MemberRow | Refusal:
        found = member_named(conn, team.team_id, ref.name) if ref.team == team.team_id else None
        if found is None or ref.generation > found.generation:
            return Refusal("unknown_member")
        return Refusal("stale_member") if ref.generation < found.generation else found

    return Target(ref.name, row)


def _recorded(events: Sequence[Event], team: TeamRow, rid: str) -> dict[str, JsonValue]:
    """The outcome the team log recorded for request `rid`: its refusal, the member it started,
    the mail it sent or the cancel it requested; an ask or a wait re-attaches, with its id."""
    request = next(
        (e for e in events if isinstance(e, OperatorRequestEvent) and e.data.request_id == rid),
        None,
    )
    wait_id = f"{events[0].branch_id}:{rid}"
    for e in events:
        if isinstance(e, WaitStartedEvent) and e.data.wait_id == wait_id:
            return {"status": "waiting", "wait_id": wait_id}
        outcome = _outcome_of(e, team, rid, None if request is None else request.event_id)
        if outcome is not None:
            return outcome
    raise AssertionError(f"request {rid} recorded no outcome this build replays")


def _outcome_of(
    e: Event, team: TeamRow, rid: str, request_event: str | None
) -> dict[str, JsonValue] | None:
    match e:
        case OperatorRefusedEvent() if e.data.request_id == rid:
            refused: dict[str, JsonValue] = {"code": e.data.code, "status": "refused"}
            if e.data.detail is not MISSING:
                refused["detail"] = to_json(e.data.detail)
            return refused
        case MemberStartedEvent() if _started_by(e, request_event):
            return {"member": to_json(e.data.member), "status": "started"}
        case MessageSentEvent() if e.data.envelope.from_ == OperatorSender(operator=rid):
            return _mail_outcome(e.data.envelope, team)
        case _:
            return None


def _started_by(e: MemberStartedEvent, request_event: str | None) -> bool:
    prov = e.data.provenance
    return prov is not MISSING and prov.root_request.event_id == request_event


def _mail_outcome(env: MailEnvelope, team: TeamRow) -> dict[str, JsonValue] | None:
    """An operator's own mail: its send's message, its ask, or its cancel's request."""
    if env.kind == "message":
        return {"id": env.mail_id, "status": "sent"}
    if env.kind == "ask" and isinstance(env.ask_id, str) and isinstance(env.deadline, int):
        return {"status": "open", "ask_id": env.ask_id, "deadline": env.deadline}
    if env.kind != "cancel" or env.to == "team_log":
        return None
    to = to_json(env.to)
    if not isinstance(to, dict):
        raise AssertionError("a cancel names a member")
    member: dict[str, JsonValue] = {"tenant": team.tenant_id, "team": env.team, **to}
    return {"member": member, "status": "cancel_requested"}
