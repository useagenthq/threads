# pyright: strict
"""Reference materialize and failed rebind (spec/schema/README.md, "Teams", "A failed rebind";
design §4.10): the team worker opens a starting member's branch with its task as the first input,
or, when the rebind fails, with one complete end-of-member append. Nothing runs a model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import obj, text
from .ops_consume import apply_cancel
from .ops_life import ended
from .team_pieces import member_log

if TYPE_CHECKING:
    from .jcs import Obj
    from .ops_world import World


def _started(w: World, name: str) -> Obj:
    return next(
        obj(e["data"])
        for log in w.logs.values()
        for e in log.events
        if e["type"] == "member_started" and obj(obj(e["data"])["member"])["name"] == name
    )


def materialize(w: World, _label: str, inp: Obj) -> Obj:
    """Re-read the row in the transaction: gone or no longer starting commits nothing."""
    name, label, rebind = text(inp["member"]), text(inp["label"]), text(inp["rebind"])
    row = w.member(name)
    if row is None or row["state"] != "starting" or label in w.logs:
        return {"status": "not_starting"}
    started = _started(w, name)
    task = next(m for m in w.rows("mail") if m["to_name"] == name and m["kind"] == "task")
    env = obj(task["envelope"])
    who = obj(obj(env["provenance"])["principal"])
    parent = obj(started["parent"])
    log = member_log(
        text(parent["event_id"]),
        text(started["agent"]),
        (text(inp["branch_id"]), text(started["thread_id"])),
    )
    pin = obj(log.events[0]["data"])
    if pin["parent"] != parent or (pin["config_hash"] != started["config_hash"] and rebind == "ok"):
        raise AssertionError("the rebuilt pin differs from member_started's")
    w.logs[label] = log
    w.appended[label] = ["thread_started"]
    task_text = obj(env["body"])["text"]
    data: Obj = {"source": "team_task", "text": task_text, "mail_id": env["mail_id"]}
    w.add(label, "user_input", data, who=who)
    cancel = next(
        (m for m in w.rows("mail") if m["to_name"] == name and m["kind"] == "cancel"), None
    )
    if cancel is not None and cancel["state"] == "pending":
        _cancelled(w, label, obj(cancel["envelope"]))
        return {"status": "cancelled"}
    if rebind == "ok":
        return {"status": "materialized"}
    _end(w, label, rebind)
    return {"status": "rebind_failed", "code": rebind}


def _cancelled(w: World, label: str, env: Obj) -> None:
    """A cancel for a starting member ends it without a rebind: the cancel is taken, the task turn
    closes as the cancellation step does, and the member ends cancelled."""
    apply_cancel(w, label, env)
    barrier = next(e for e in reversed(w.logs[label].events) if e["type"] == "cancel_requested")
    w.add(label, "cancelled", {"request_event_id": barrier["event_id"]})
    w.add(label, "turn_completed", {"reason": "cancelled"})
    ended(w, label, {"status": "cancelled"})


def _end(w: World, label: str, code: str) -> None:
    """The failed rebind's end: the task turn closes before any model request, then the member
    ends failed, with everything a member's end carries."""
    w.add(label, "turn_completed", {"reason": "error", "code": code})
    ended(
        w, label, {"status": "failed", "error": {"code": code, "message": f"rebind failed: {code}"}}
    )
