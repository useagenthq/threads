# pyright: strict
"""Reference materialize and failed rebind (spec/schema/README.md, "Teams", "A failed rebind";
design §4.10): the team worker opens a starting member's branch with its task as the first input,
or, when the rebind fails, with one complete end-of-member append. Nothing runs a model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import eid, obj, text
from .team_pieces import Route, at, envelope, failed, member_log

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj
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
    if rebind == "ok":
        return {"status": "materialized"}
    _end(w, label, row, rebind, env)
    return {"status": "rebind_failed", "code": rebind}


def _end(w: World, label: str, row: Obj, code: str, task: Obj) -> None:
    """turn_completed{error}, member_ended{failed}, one member_ended notification per monitor
    on this generation, and a mail_refused plus a bounce for every other pending inbound mail."""
    ref = w.ref(row)
    prov = obj(task["provenance"])
    log = w.logs[label]
    w.add(label, "turn_completed", {"reason": "error", "code": code})
    ended = failed(ref, code)
    end = w.add(label, "member_ended", {"result": ended})

    def next_id() -> str:
        return f"{log.branch}:{eid(log.seq + 1, log.branch)}"

    watched = [
        m
        for m in w.rows("monitors")
        if m["target_name"] == row["name"] and m["target_generation"] == row["generation"]
    ]
    for m in watched:
        to = w.address(text(m["watcher_branch_id"]))
        notice = envelope(
            next_id(),
            "member_ended",
            Route(ref, to, prov),
            at(text(end["event_id"]), log.thread),
            monitor_id=m["monitor_id"],
            result=ended,
        )
        w.add(label, "message_sent", {"envelope": notice})
    for mail in w.pending(text(row["name"])):
        refused = obj(mail["envelope"])
        why = w.add(label, "mail_refused", {"mail_id": refused["mail_id"], "code": "member_ended"})
        extra: dict[str, JsonValue] = {}
        if refused["kind"] == "ask":
            extra = {"ask_id": refused["ask_id"], "result": ended}
        sender = obj(refused["from"])
        back = "team_log" if "operator" in sender else text(sender["name"])
        bounce = envelope(
            next_id(),
            "bounce",
            Route(ref, back, prov),
            at(text(why["event_id"]), log.thread),
            code="member_ended",
            **extra,
        )
        w.add(label, "message_sent", {"envelope": bounce})
