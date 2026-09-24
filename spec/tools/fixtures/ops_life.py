# pyright: strict
"""A member's own settling appends (spec/schema/README.md, "Teams", "Settling"; design §4.11):
idle fires its settle monitors and its unfired task monitor; an end fires every monitor, refuses
every pending inbound mail with a bounce and, for a lead, cancels every live member. Monitors fire
in monitor_id order, refused mail in (created_at, mail_id) order."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, eid, obj, text
from .team_pieces import Route, at, body, envelope

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj
    from .ops_world import World

FIRES = {"member_idle": ("settle", "task"), "member_ended": ("settle", "task", "end")}


def _next_id(w: World, label: str) -> str:
    log = w.logs[label]
    return f"{log.branch}:{eid(log.seq + 1, log.branch)}"


def _fire(w: World, label: str, settled: Obj, result: Obj) -> None:
    """One notification per monitor row on this member whose kind this event fires."""
    row = w.own_row(label)
    if row is None:
        return
    kind = "member_settled" if settled["type"] == "member_idle" else "member_ended"
    prov = w.turn_provenance(label)
    log = w.logs[label]
    for m in w.rows("monitors"):
        if m["target_name"] != row["name"] or m["target_generation"] != row["generation"]:
            continue
        if m["kind"] not in FIRES[text(settled["type"])]:
            continue
        notice = envelope(
            _next_id(w, label),
            kind,
            Route(w.ref(row), w.address(text(m["watcher_branch_id"])), prov),
            at(text(settled["event_id"]), log.thread),
            monitor_id=m["monitor_id"],
            result=result,
        )
        w.add(label, "message_sent", {"envelope": notice})


def idle(w: World, label: str, _inp: Obj) -> Obj:
    """The task turn's end_turn: turn_completed, member_idle with the last answer, and its
    notifications, in one append."""
    log = w.logs[label]
    answer = next(e for e in reversed(log.events) if e["type"] == "model_response")
    parts = [obj(p) for p in arr(obj(answer["data"])["content"])]
    said = "".join(text(p["text"]) for p in parts if p["type"] == "text")
    w.add(label, "turn_completed", {"reason": "end_turn"})
    result: Obj = {"member": w.caller(label), "status": "completed", "output": body(said)}
    settled = w.add(label, "member_idle", {"result": result})
    _fire(w, label, settled, result)
    return {"status": "idle", "result": result}


def end(w: World, label: str, inp: Obj) -> Obj:
    """A terminal turn end: turn_completed{reason}, member_ended{result}, its notifications, a
    mail_refused plus a bounce per pending inbound mail, and for a lead one cancel per live
    member."""
    turn: Obj = {"reason": inp["reason"]}
    if "code" in inp:
        turn["code"] = inp["code"]
    w.add(label, "turn_completed", turn)
    ended(w, label, obj(inp["result"]))
    return {"status": "ended"}


def ended(w: World, label: str, outcome: Obj) -> Obj:
    """member_ended and everything its append carries; the turn is already closed."""
    row = w.own_row(label)
    if row is None:
        raise AssertionError(label)
    result: Obj = {"member": w.ref(row), **outcome}
    settled = w.add(label, "member_ended", {"result": result})
    _fire(w, label, settled, result)
    _refuse(w, label, row, result)
    if row["role"] == "lead":
        _close(w, label, settled)
    return result


def _refuse(w: World, label: str, row: Obj, result: Obj) -> None:
    """Every pending inbound mail is returned. Its bounce carries the refused mail's provenance,
    so it belongs to the request that sent it; an open ask's bounce carries the result."""
    log = w.logs[label]
    for mail in w.pending(text(row["name"])):
        refused = obj(mail["envelope"])
        why = w.add(label, "mail_refused", {"mail_id": refused["mail_id"], "code": "member_ended"})
        extra: dict[str, JsonValue] = {}
        if refused["kind"] == "ask":
            extra = {"ask_id": refused["ask_id"], "result": result}
        sender = obj(refused["from"])
        back = "team_log" if "operator" in sender else text(sender["name"])
        bounce = envelope(
            _next_id(w, label),
            "bounce",
            Route(w.ref(row), back, obj(refused["provenance"])),
            at(text(why["event_id"]), log.thread),
            code="member_ended",
            **extra,
        )
        w.add(label, "message_sent", {"envelope": bounce})


def _close(w: World, label: str, settled: Obj) -> None:
    """A lead's end closes the team: one cancel per live member, in name order."""
    log = w.logs[label]
    prov = w.turn_provenance(label)
    lead = w.caller(label)
    live = sorted(
        (r for r in w.rows("team_members") if r["role"] == "member" and r["state"] != "ended"),
        key=lambda r: text(r["name"]),
    )
    for r in live:
        note = envelope(
            _next_id(w, label),
            "cancel",
            Route(lead, text(r["name"]), prov),
            at(text(settled["event_id"]), log.thread),
        )
        w.add(label, "message_sent", {"envelope": note})
