# pyright: strict
"""Reference ops that send mail (spec/schema/README.md, "Teams"; design §4.5, §4.8 open, §4.9 and
§4.14 request): send, ask, reply and cancel, for a model call or an operator request."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, num, obj, text
from .ops_request import Request, open_request, park, same_tenant
from .ops_world import DEFAULT_MS, TEAM_LOG, Refused
from .team_pieces import Route, body, envelope

if TYPE_CHECKING:
    from .jcs import Obj
    from .ops_world import World


def _deliverable(req: Request, op: str) -> Obj:
    """send and ask, after the policy: team open, the member known at its generation, not ended,
    not the sender, and its mailbox not full."""
    w = req.w
    to = req.args["to"]
    name = text(obj(to)["name"]) if req.operator else text(to)
    req.decide(op, name, allow=not req.operator or same_tenant(req))
    if w.team()["closed_at"] is not None:
        raise Refused("team_closed")
    row = req.target(to)
    if row["state"] == "ended":
        raise Refused("member_ended")
    if req.is_self(row):
        raise Refused("self")
    if len(w.pending(text(row["name"]))) >= w.mailbox:
        raise Refused("mailbox_full")
    return row


def send(w: World, label: str, inp: Obj) -> Obj:
    req = open_request(w, label, "send", inp)
    try:
        row = _deliverable(req, "send")
    except Refused as r:
        return req.refuse(r.code)
    route = Route(req.sender, text(row["name"]), req.prov)
    note = envelope(req.mail_id, "message", route, req.causal, body=body(text(req.args["text"])))
    w.add(label, "message_sent", {"envelope": note})
    return req.done({"id": req.mail_id, "status": "sent"})


def ask(w: World, label: str, inp: Obj) -> Obj:
    """ask.open: send with kind ask, a deadline, headroom on the recipient's budgets, and a park
    for a member asker whose turn has nothing else to run."""
    req = open_request(w, label, "ask", inp)
    try:
        row = _deliverable(req, "ask")
        if not w.headroom:
            raise Refused("budget_exceeded")
    except Refused as r:
        return req.refuse(r.code)
    timeout = min(num(req.args.get("timeout_ms", DEFAULT_MS)), DEFAULT_MS)
    deadline = w.now + timeout
    route = Route(req.sender, text(row["name"]), req.prov)
    question = body(text(req.args["question"]))
    mail = envelope(
        req.mail_id, "ask", route, req.causal, ask_id=req.mail_id, deadline=deadline, body=question
    )
    w.add(label, "message_sent", {"envelope": mail})
    if not req.operator and arr(w.state(label)["pending_calls"]) == [req.key["call_id"]]:
        park(w, label, {"kind": "ask", "id": req.mail_id})
    return {"status": "open", "ask_id": req.mail_id, "deadline": deadline}


def reply(w: World, label: str, inp: Obj) -> Obj:
    """reply: the ask was delivered here, not yet replied to, and its row is open before its
    deadline. No policy decision: only an asked member replies."""
    req = open_request(w, label, "reply", inp)
    ask_id = text(req.args["ask_id"])
    log = w.logs[label]
    mail = {
        t: [obj(obj(e["data"])["envelope"]) for e in log.events if e["type"] == t]
        for t in ("message_received", "message_sent")
    }
    asked = next(
        (m for m in mail["message_received"] if m["kind"] == "ask" and m["ask_id"] == ask_id), None
    )
    row = next((a for a in w.rows("asks") if a["ask_id"] == ask_id), None)
    try:
        if asked is None or row is None:
            raise Refused("unknown_ask")
        if any(m["kind"] == "reply" and m["ask_id"] == ask_id for m in mail["message_sent"]):
            raise Refused("already_replied")
        if row["state"] != "open" or w.now >= num(row["deadline"]):
            raise Refused("ask_closed")
    except Refused as r:
        return req.refuse(r.code)
    sender = obj(asked["from"])
    to = TEAM_LOG if "operator" in sender else text(sender["name"])
    route = Route(req.sender, to, obj(asked["provenance"]))
    answer = body(text(req.args["text"]))
    w.add(
        label,
        "message_sent",
        {"envelope": envelope(req.mail_id, "reply", route, req.causal, ask_id=ask_id, body=answer)},
    )
    return req.done({"id": req.mail_id, "status": "sent"})


def cancel(w: World, label: str, inp: Obj) -> Obj:
    """cancel's request: durable intent. The target's starter or the operator may; the member
    is known and not ended."""
    req = open_request(w, label, "cancel", inp)
    to = req.args["member"]
    name = text(obj(to)["name"]) if req.operator else text(to)
    known = w.member(name)
    try:
        grant = (
            same_tenant(req) if req.operator else known is not None and w.starter(known) == label
        )
        req.decide("cancel", name, allow=grant)
        row = req.target(to)
        if row["state"] == "ended":
            raise Refused("member_ended")
    except Refused as r:
        return req.refuse(r.code)
    route = Route(req.sender, name, req.prov)
    w.add(label, "message_sent", {"envelope": envelope(req.mail_id, "cancel", route, req.causal)})
    return req.done({"member": w.ref(row), "status": "cancel_requested"})
