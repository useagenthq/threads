# pyright: strict
"""Reference store ops of Teams Phase 2 (spec/schema/README.md, "Teams Phase 2"): a caller's send
and ask of a host member under the host rules, a host member's reply to a caller, a caller's
consume, a host member's failed turn, the supervisor's step, and deleting a caller. ops_run.py
runs these for a world whose team log is a host team's."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, eid, num, obj, text
from .ops_consume import close_ask
from .ops_world import DEFAULT_MS, Refused
from .team_pieces import body

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj
    from .ops_world import World


def _agent(w: World, label: str) -> str:
    return text(obj(w.logs[label].events[0]["data"])["agent_name"])


def _address(w: World, label: str) -> Obj:
    log = w.logs[label]
    return {"caller": {"thread_id": log.thread, "branch_id": log.branch, "agent": _agent(w, label)}}


def _decide(w: World, label: str, op: str, target: str, call_id: str) -> None:
    """A host rule, else default deny, logged as message_policy_decided (never source team)."""
    agent = _agent(w, label)
    rule = next(
        (r for r in w.rules if r["from"] == agent and r["to"] == target and op in arr(r["allow"])),
        None,
    )
    data: Obj = {"op": op, "decision": "allow" if rule else "deny", "target": target}
    data |= (
        {"source": "message_policy", "rule": {"from": agent, "to": target}}
        if rule
        else {"source": "default"}
    )
    w.add(label, "message_policy_decided", {**data, "call_id": call_id})
    if rule is None:
        raise Refused("forbidden")


def _mail(w: World, label: str, kind: str, inp: Obj) -> Obj:
    """A caller's send or ask of a host member: the call's decision, then the envelope."""
    cid, args = text(inp["call_id"]), obj(inp["args"])
    call = w.call(label, cid, args)
    name = text(args["to"])
    _decide(w, label, "send" if kind == "message" else kind, name, cid)
    row = w.member(name)
    if row is None or row["role"] != "host_member":
        raise Refused("unknown_member")
    if row["state"] == "ended":
        raise Refused("member_ended")
    mail = f"{w.logs[label].branch}:{cid}"
    env: Obj = {
        "mail_id": mail,
        "kind": kind,
        "team": row["team_id"],
        "from": _address(w, label),
        "to": {"name": name, "generation": row["generation"]},
        "provenance": w.turn_provenance(label),
        "causal": {"thread_id": w.logs[label].thread, "event_id": call["event_id"]},
    }
    if kind == "ask":
        env |= {"ask_id": mail, "deadline": w.now + DEFAULT_MS}
    env["body"] = body(text(args["question" if kind == "ask" else "text"]))
    w.add(label, "message_sent", {"envelope": env})
    return env


def send(w: World, label: str, inp: Obj) -> Obj:
    cid = text(inp["call_id"])
    try:
        env = _mail(w, label, "message", inp)
    except Refused as r:
        return _refuse(w, label, cid, r.code)
    value: Obj = {"id": env["mail_id"], "status": "sent"}
    w.answer(label, cid, value)
    return value


def ask(w: World, label: str, inp: Obj) -> Obj:
    cid = text(inp["call_id"])
    try:
        env = _mail(w, label, "ask", inp)
    except Refused as r:
        return _refuse(w, label, cid, r.code)
    address: Obj = {"kind": "ask", "id": env["ask_id"]}
    w.add(label, "parked", {"address": address, "reason": "awaiting_member"})
    return {"status": "open", "ask_id": env["ask_id"], "deadline": env["deadline"]}


def _own(w: World, label: str) -> Obj:
    row = w.own_row(label)
    if row is None:
        raise AssertionError(f"{label} is not a host member")
    return row


def _refuse(w: World, label: str, cid: str, code: str) -> Obj:
    value: Obj = {"code": code, "status": "refused"}
    w.answer(label, cid, value)
    return value


def _received_asks(w: World, label: str) -> list[Obj]:
    return [
        obj(obj(e["data"])["envelope"])
        for e in w.logs[label].events
        if e["type"] == "message_received" and obj(obj(e["data"])["envelope"])["kind"] == "ask"
    ]


def _answered(w: World, label: str) -> set[str]:
    sent = [
        obj(obj(e["data"])["envelope"]) for e in w.logs[label].events if e["type"] == "message_sent"
    ]
    return {text(env["ask_id"]) for env in sent if "ask_id" in env}


def reply(w: World, label: str, inp: Obj) -> Obj:
    """A host member's reply: to the asker's address as the ask carries it, a caller's too."""
    cid, args = text(inp["call_id"]), obj(inp["args"])
    call = w.call(label, cid, args)
    asked = next((a for a in _received_asks(w, label) if a["ask_id"] == args["ask_id"]), None)
    if asked is None or asked["ask_id"] in _answered(w, label):
        return _refuse(w, label, cid, "unknown_ask" if asked is None else "already_replied")
    own = _own(w, label)
    env: Obj = {
        "mail_id": f"{w.logs[label].branch}:{cid}",
        "kind": "reply",
        "team": asked["team"],
        "from": w.ref(own),
        "to": asked["from"],
        "provenance": asked["provenance"],
        "causal": {"thread_id": w.logs[label].thread, "event_id": call["event_id"]},
        "ask_id": asked["ask_id"],
        "body": body(text(args["text"])),
    }
    w.add(label, "message_sent", {"envelope": env})
    value: Obj = {"id": env["mail_id"], "status": "sent"}
    w.answer(label, cid, value)
    return value


def consume(w: World, label: str, _inp: Obj) -> Obj:
    """A caller's consume: its pending replies and ask bounces, each closing its ask."""
    branch = w.logs[label].branch
    rows = sorted(
        (m for m in w.rows("mail") if m["state"] == "pending" and m["to_branch_id"] == branch),
        key=lambda m: (num(m["created_at"]), text(m["mail_id"])),
    )
    for m in rows:
        env = obj(m["envelope"])
        who = obj(obj(env["provenance"])["principal"])
        got = w.add(
            label, "message_received", {"mail_id": env["mail_id"], "envelope": env}, who=who
        )
        outcome: Obj
        if env["kind"] == "reply":
            outcome = {"status": "answered", "reply": env["mail_id"]}
        elif env.get("code") == "turn_failed":
            outcome = {"status": "failed", "error": env["error"]}
        else:
            outcome = {"status": "member_ended", "result": env["result"]}
        close_ask(w, label, text(env["ask_id"]), outcome, text(got["event_id"]))
    return {"status": "consumed", "mail_ids": [m["mail_id"] for m in rows]}


def turn_failure(w: World, label: str, inp: Obj) -> Obj:
    """A host member's turn ends other than end_turn or cancelled: only the turn ends. Each ask
    the turn took and left unanswered bounces turn_failed, then member_idle{turn_failed}."""
    hop = inp.get("hop")
    if hop is not None:
        w.add(
            label, "budget_exceeded", {**obj(hop), "scope": "hop", "observed_is_upper_bound": False}
        )
    end = w.add(label, "turn_completed", obj(inp["turn"]))
    error = obj(inp["error"])
    own = _own(w, label)
    failed: list[JsonValue] = []
    for asked in _turn_asks(w, label):
        log = w.logs[label]
        env: Obj = {
            "mail_id": f"{log.branch}:{eid(log.seq + 1, log.branch)}",
            "kind": "bounce",
            "team": asked["team"],
            "from": w.ref(own),
            "to": asked["from"],
            "provenance": asked["provenance"],
            "causal": {"thread_id": log.thread, "event_id": end["event_id"]},
            "ask_id": asked["ask_id"],
            "code": "turn_failed",
            "error": error,
        }
        w.add(label, "message_sent", {"envelope": env})
        failed.append(asked["ask_id"])
    w.add(label, "member_idle", {"turn_failed": error})
    return {"status": "idle", "failed": failed}


def _turn_asks(w: World, label: str) -> list[Obj]:
    """The asks received since the member's last turn end, and not yet answered."""
    events = w.logs[label].events[:-1]  # before this turn's turn_completed
    start = max((i for i, e in enumerate(events) if e["type"] == "member_idle"), default=0)
    answered = _answered(w, label)
    return [
        obj(obj(e["data"])["envelope"])
        for e in events[start:]
        if e["type"] == "message_received"
        and obj(obj(e["data"])["envelope"])["kind"] == "ask"
        and obj(obj(e["data"])["envelope"])["ask_id"] not in answered
    ]
