"""The model tools ask and reply (spec/schema/README.md, "Teams"; design §4.8 open and §4.9), each
decided inside the caller's append. An ask stays a pending call until the asker's writer closes it
(close.py); a reply is mail to the asker. Reference: spec/tools/fixtures/ops_send.py."""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import JsonValue

from threads.log import (
    CallerAddress,
    MailEnvelope,
    MemberRef,
    MessageReceivedEvent,
    MessageSentEvent,
)
from threads.reduce.handlers import to_json
from threads.team.call import (
    CallContext,
    Refusal,
    call_mail_id,
    call_request,
    caller_of,
    causal_of,
    named,
    recorded,
)
from threads.team.constants import TEAM_CONSTANTS
from threads.team.mail import body_of, sent
from threads.team.ops import TeamLimits, deliverable
from threads.team.park import park_call
from threads.team.request import Request, Target
from threads.team.rows import MemberRow, ask_row


@dataclass(frozen=True, slots=True)
class AskPlan:
    """What ask reads besides the store."""

    limits: TeamLimits
    headroom: Callable[[MemberRow], bool]
    """Every budget covering the recipient has room for one request of its model."""
    timeout_ms: int = TEAM_CONSTANTS.ask_wait_default_ms
    """The ask's deadline from now, capped by the default (the model's ask takes none)."""


def _sent_as(ctx: CallContext, mail_id: str) -> MailEnvelope | None:
    """The mail this log already sent under `mail_id`: a re-dispatched call's own."""
    return next(
        (
            e.data.envelope
            for e in ctx.fold.events
            if isinstance(e, MessageSentEvent) and e.data.envelope.mail_id == mail_id
        ),
        None,
    )


def ask(ctx: CallContext, to: str, question: str, plan: AskPlan) -> JsonValue:
    """ask.open: send with kind ask, a deadline and headroom on the recipient's budgets; the
    asker's call stays pending and parks once its turn has nothing else to run. A re-dispatched
    ask only parks. Returns the open ask, or the refusal it recorded."""
    caller = caller_of(ctx)
    ask_id = call_mail_id(ctx)
    opened = _sent_as(ctx, ask_id)
    if opened is not None:
        if not isinstance(opened.deadline, int):
            raise AssertionError("an ask envelope always has a deadline")
        park_call(ctx, caller.provenance)
        return {"status": "open", "ask_id": ask_id, "deadline": opened.deadline}
    got = open_ask(call_request(ctx), named(ctx, to), question, plan)
    if got.get("status") == "open":
        park_call(ctx, caller.provenance)
    return got


def open_ask(req: Request, to: Target, question: str, plan: AskPlan) -> dict[str, JsonValue]:
    """The ask's mail, for a model call or an operator request: the checks send makes, headroom,
    then message_sent{ask} with its deadline. Nothing records the open ask as a result."""
    row = deliverable(req, "ask", to, plan.limits)
    if isinstance(row, Refusal):
        return req.refuse(row)
    if not plan.headroom(row):
        return req.refuse(Refusal("budget_exceeded"))
    cap = TEAM_CONSTANTS.ask_wait_default_ms
    deadline = req.batch.now + min(plan.timeout_ms, cap)
    env: dict[str, JsonValue] = {
        "mail_id": req.mail_id,
        "kind": "ask",
        "team": req.team.team_id,
        "from": req.sender,
        "to": {"name": row.name, "generation": row.generation},
        "provenance": req.provenance,
        "causal": req.causal,
        "ask_id": req.mail_id,
        "deadline": deadline,
        "body": body_of(question, req.put),
    }
    req.batch.add(sent(env))
    return {"status": "open", "ask_id": req.mail_id, "deadline": deadline}


def reply(ctx: CallContext, ask_id: str, text: str) -> JsonValue:
    """reply: the ask was delivered here, not replied to yet, and its row is open before its
    deadline, read in this transaction. No policy decision: only an asked member replies."""
    caller = caller_of(ctx)
    events = ctx.fold.events
    asked = next(
        (
            e.data.envelope
            for e in events
            if isinstance(e, MessageReceivedEvent)
            and e.data.envelope.kind == "ask"
            and e.data.envelope.ask_id == ask_id
        ),
        None,
    )
    row = ask_row(ctx.conn, ask_id)
    if asked is None or row is None:
        return recorded(ctx, Refusal("unknown_ask"))
    replied = any(
        isinstance(e, MessageSentEvent)
        and e.data.envelope.kind == "reply"
        and e.data.envelope.ask_id == ask_id
        for e in events
    )
    if replied:
        return recorded(ctx, Refusal("already_replied"))
    if row.state != "open" or ctx.batch.now >= row.deadline:
        return recorded(ctx, Refusal("ask_closed"))
    sender = asked.from_
    to: JsonValue = (
        {"name": sender.name, "generation": sender.generation}
        if isinstance(sender, MemberRef)
        else to_json(sender)
        if isinstance(sender, CallerAddress)
        else "team_log"
    )
    mail_id = call_mail_id(ctx)
    env: dict[str, JsonValue] = {
        "mail_id": mail_id,
        "kind": "reply",
        "team": caller.team.team_id,
        "from": caller.ref,
        "to": to,
        "provenance": to_json(asked.provenance),
        "causal": causal_of(ctx),
        "ask_id": ask_id,
        "body": body_of(text, ctx.put),
    }
    ctx.batch.add(sent(env))
    return recorded(ctx, {"id": mail_id, "status": "sent"})
