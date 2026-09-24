# pyright: strict
"""Reference mail.consume, ask.complete, cancel application and the deadline step (spec/schema/
README.md, "Teams"; design §4.7, §4.8 complete, §4.12 deadline, §4.14 application). Control mail
is always taken; ordinary mail is taken as one homogeneous batch, never while parked."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, num, obj, text
from .ops_observe import call_of, finish
from .ops_world import TEAM_LOG, public
from .ref_rules import principal

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj
    from .ops_world import World

NOTICES = ("member_settled", "member_ended")


def _receive(w: World, label: str, env: Obj) -> Obj:
    who = obj(obj(env["provenance"])["principal"])
    return w.add(label, "message_received", {"mail_id": env["mail_id"], "envelope": env}, who=who)


def close_ask(w: World, label: str, ask_id: str, outcome: Obj, cause: str | None) -> Obj:
    """ask.complete: ask_closed, and for a member asker its resumed and the call's one result."""
    closed = w.add(label, "ask_closed", {"ask_id": ask_id, "outcome": outcome})
    status = text(outcome["status"])
    value: Obj = {"ask_id": ask_id, "status": status}
    if status == "answered":
        reply = next(m for m in w.rows("mail") if m["mail_id"] == outcome["reply"])
        env = obj(reply["envelope"])
        value |= {"member": env["from"], "text": obj(env["body"])["text"]}
    elif status == "member_ended":
        value["result"] = public(outcome["result"])
    if not w.is_team_log(label):
        address: Obj = {"kind": "ask", "id": ask_id}
        if address in arr(w.state(label)["parked"]):
            w.add(
                label,
                "resumed",
                {"address": address, "cause_event_id": cause or closed["event_id"]},
            )
        w.answer(label, call_of(ask_id), value)
    return value


def _open_ask(w: World, label: str, ask_id: JsonValue) -> bool:
    branch = w.logs[label].branch
    return any(
        a["ask_id"] == ask_id and a["asker_branch_id"] == branch and a["state"] == "open"
        for a in w.rows("asks")
    )


def _waits(w: World, label: str) -> dict[str, str]:
    """This log's open waits: settle MonitorId -> WaitId."""
    log, out = w.logs[label], dict[str, str]()
    for e in log.events:
        d = obj(e["data"])
        if e["type"] == "wait_started":
            for m in arr(d["members"]):
                out[f"{log.branch}:{e['event_id']}:{text(obj(m)['name'])}"] = text(d["wait_id"])
        elif e["type"] == "wait_finished":
            out = {k: v for k, v in out.items() if v != d["wait_id"]}
    return out


def _control(w: World, label: str, env: Obj) -> bool:
    """Control mail, the exhaustive list, applied now; False for ordinary mail."""
    kind = text(env["kind"])
    member_park: Obj = {"kind": "member", "id": env.get("monitor_id")}
    team_log = w.is_team_log(label)
    if kind == "cancel":
        _cancel(w, label, env)
    elif kind in ("reply", "bounce") and "ask_id" in env and _open_ask(w, label, env["ask_id"]):
        _answered(w, label, env)
    elif kind in NOTICES and env["monitor_id"] in _waits(w, label):
        got = _receive(w, label, env)
        finish(w, label, _waits(w, label)[text(env["monitor_id"])], text(got["event_id"]))
    elif kind in NOTICES and (team_log or member_park in arr(w.state(label)["parked"])):
        got = _receive(w, label, env)
        if not team_log:
            w.add(label, "resumed", {"address": member_park, "cause_event_id": got["event_id"]})
    elif kind == "member_parked":
        live = any(m["monitor_id"] == env["monitor_id"] for m in w.rows("monitors"))
        _receive(w, label, env)
        if live and not team_log:
            w.add(label, "parked", {"address": member_park, "reason": "awaiting_member"})
    elif team_log:
        _receive(w, label, env)  # the team log takes everything, and opens no turn
    else:
        return False
    return True


def _answered(w: World, label: str, env: Obj) -> Obj:
    """A reply or an ask's bounce closes its open ask: answered, or member_ended."""
    got = _receive(w, label, env)
    outcome: Obj = (
        {"status": "answered", "reply": env["mail_id"]}
        if env["kind"] == "reply"
        else {"status": "member_ended", "result": env["result"]}
    )
    return close_ask(w, label, text(env["ask_id"]), outcome, text(got["event_id"]))


def _cancel(w: World, label: str, env: Obj) -> None:
    """Applying a cancel: its receipt and today's barrier, cancel_requested{scope: tree}, in one
    append; an asker parked on an open ask closes it cancelled. The barrier rules end the member."""
    got = _receive(w, label, env)
    who = obj(obj(env["provenance"])["principal"])
    w.add(label, "cancel_requested", {"scope": "tree"}, who=who)
    for p in arr(w.state(label)["parked"]):
        address = obj(p)
        if address["kind"] == "ask" and _open_ask(w, label, address["id"]):
            close_ask(w, label, text(address["id"]), {"status": "cancelled"}, text(got["event_id"]))


def _pair(prov: JsonValue) -> tuple[str, str]:
    """(principal, root_request): one turn's authority and budget root."""
    p = obj(prov)
    root = obj(p["root_request"])
    return (principal(p["principal"]), f"{text(root['thread_id'])}:{text(root['event_id'])}")


def consume(w: World, label: str, _inp: Obj) -> Obj:
    """mail.consume: the recipient's pending rows in (created_at, mail_id) order. Mid-turn only
    rows of the open turn's (principal, root_request); else the longest prefix sharing the first
    row's pair."""
    row = w.own_row(label)
    to = TEAM_LOG if row is None else text(row["name"])
    turn = _pair(w.turn_provenance(label)) if w.state(label)["status"] == "in_turn" else None
    taken: list[JsonValue] = []
    batch: tuple[str, str] | None = None
    stopped = False
    rows = w.pending(to)
    if not rows:
        return {"status": "nothing_pending"}
    for m in rows:
        env = obj(m["envelope"])
        if _control(w, label, env):
            taken.append(env["mail_id"])
            continue
        if stopped or arr(w.state(label)["parked"]):
            continue  # a parked recipient leaves ordinary mail pending
        pair = _pair(env["provenance"])
        if turn is not None and pair != turn:
            continue
        if turn is None and batch is not None and pair != batch:
            stopped = True
            continue
        batch = pair
        _receive(w, label, env)
        taken.append(env["mail_id"])
    return {"status": "consumed", "mail_ids": taken}


def deadline(w: World, label: str, inp: Obj) -> Obj:
    """The team worker's deadline step for one ask or wait of label, under its writer."""
    key = text(inp["id"])
    ask = next((a for a in w.rows("asks") if a["ask_id"] == key), None)
    if ask is not None:
        if ask["state"] != "open" or w.now < num(ask["deadline"]):
            return {"status": "not_due"}
        to = w.address(w.logs[label].branch)
        mine = [obj(m["envelope"]) for m in w.pending(to)]
        for kind in ("reply", "bounce"):  # the decision order: a reply, then a bounce
            env = next((e for e in mine if e["kind"] == kind and e.get("ask_id") == key), None)
            if env is not None:
                return _answered(w, label, env)
        return close_ask(w, label, key, {"status": "timed_out"}, None)
    started = next(
        e
        for e in w.logs[label].events
        if e["type"] == "wait_started" and obj(e["data"])["wait_id"] == key
    )
    if w.now < num(obj(started["data"])["deadline"]) or key not in _waits(w, label).values():
        return {"status": "not_due"}
    monitors = {k for k, v in _waits(w, label).items() if v == key}
    for m in w.pending(w.address(w.logs[label].branch)):
        env = obj(m["envelope"])
        if env["kind"] in NOTICES and env["monitor_id"] in monitors:
            _receive(w, label, env)  # committed before this step: it counts
    return finish(w, label, key, None, deadline=True)
