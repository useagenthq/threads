# pyright: strict
"""Reference supervisor step and caller deletion of Teams Phase 2 (spec/schema/README.md, "Teams
Phase 2", rule 51 and "Deleting a thread"): the host team log's writer decides once on each ended
host member generation, and a caller is deleted only when nothing of it is live."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import num, obj, text
from .host_pieces import host_start

if TYPE_CHECKING:
    from .jcs import Obj
    from .ops_world import World


def supervise(w: World, label: str, inp: Obj) -> Obj:
    """One decision on the named member's ended generation, in the team log: restart when the
    policy is on_failure, the member failed and the window holds fewer restarts than the cap;
    else stop. An end already decided is left alone (exactly once)."""
    name, policy = text(inp["member"]), obj(inp["policy"])
    row = w.member(name)
    if row is None or row["state"] != "ended":
        return {"status": "nothing_ended"}
    gen = num(row["generation"])
    log = w.logs[label]
    decisions = [
        obj(e["data"]) | {"time": e["time"]}
        for e in log.events
        if e["type"] == "supervisor_decided"
    ]
    mine = [d for d in decisions if obj(d["member"])["name"] == name]
    if any(num(obj(d["member"])["generation"]) == gen for d in mine):
        return {"status": "already_decided"}
    window = num(policy["within_ms"])
    count = sum(1 for d in mine if d["action"] == "restart" and w.now - num(d["time"]) < window)
    end = next(
        e
        for e in reversed(w.logs[w.label_of(text(row["branch_id"]))].events)
        if e["type"] == "member_ended"
    )
    failed = obj(obj(end["data"])["result"])["status"] == "failed"
    allowed = policy["restart"] == "on_failure" and count < num(policy["max_restarts"])
    action = "restart" if allowed and failed else "stop"
    data: Obj = {
        "member": w.ref(row),
        "ended": {"branch_id": end["branch_id"], "seq": end["seq"]},
        "action": action,
        "restarts_in_window": count,
        "policy": policy,
    }
    w.add(label, "supervisor_decided", data)
    if action == "restart":
        w.add(label, "member_started", host_start(gen + 1, restart_of=gen))
    return {"status": "decided", "action": action, "restarts_in_window": count}


def delete(w: World, label: str, _inp: Obj) -> Obj:
    """Deleting a caller: busy while it asks (an open ask), has a reply or bounce unconsumed, or
    sent a host member mail not yet consumed; else its log goes, and a rebuild skips every row
    naming it. mail has no sender column: the caller's sent rows are the mail ids of its own log's
    message_sent events."""
    log = w.logs[label]
    sent = {
        text(obj(obj(e["data"])["envelope"])["mail_id"])
        for e in log.events
        if e["type"] == "message_sent"
    }
    live = any(a["asker_branch_id"] == log.branch and a["state"] == "open" for a in w.rows("asks"))
    for m in w.rows("mail"):
        mine = m["mail_id"] in sent
        live = live or (m["state"] == "pending" and (m["to_branch_id"] == log.branch or mine))
    if live:
        return {"status": "refused", "code": "busy"}
    del w.logs[label]
    w.deleted.add(log.thread)
    return {"status": "deleted"}
