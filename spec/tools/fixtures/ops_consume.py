# pyright: strict
"""Reference mail.consume, ask.complete, cancel application and the deadline step (spec/schema/
README.md, "Teams"; design §4.7, §4.8 complete, §4.12 deadline, §4.14 application). Control mail
is always taken; ordinary mail is taken as one homogeneous batch, and only when the recipient was
not parked when the consume began."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, num, obj, text
from .ops_life import bounce
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


def _mine(w: World, label: str) -> list[Obj]:
    return [obj(m["envelope"]) for m in w.pending(w.address(w.logs[label].branch))]


def _still_pending(w: World, label: str, mail_id: JsonValue) -> bool:
    return any(e["mail_id"] == mail_id for e in _mine(w, label))


def close_ask(w: World, label: str, ask_id: str, outcome: Obj, cause: str | None) -> Obj:
    """ask_closed, and for a member asker its resumed and the call's one result."""
    closed = w.add(label, "ask_closed", {"ask_id": ask_id, "outcome": outcome})
    status = text(outcome["status"])
    value: Obj = {"ask_id": ask_id, "status": status}
    if status == "answered":
        reply = next(m for m in w.rows("mail") if m["mail_id"] == outcome["reply"])
        env = obj(reply["envelope"])
        value |= {"member": env["from"], "text": obj(env["body"])["text"]}
    elif status == "member_ended":
        value["result"] = public(outcome["result"])
    elif status == "failed":  # a host member's failed turn (Teams Phase 2)
        value["error"] = outcome["error"]
    if not w.is_team_log(label):
        address: Obj = {"kind": "ask", "id": ask_id}
        if address in arr(w.state(label)["parked"]):
            data: Obj = {"address": address, "cause_event_id": cause or closed["event_id"]}
            w.add(label, "resumed", data)
        w.answer(label, call_of(ask_id), value)
    return value


def complete(w: World, label: str, ask_id: str, *, cancelled: bool, due: bool) -> Obj | None:
    """ask.complete's one decision, whoever triggers it: a pending reply, then a pending ask
    bounce, then a cancel (or, for the team log, a closed team), then the deadline."""
    for kind in ("reply", "bounce"):
        env = next(
            (e for e in _mine(w, label) if e["kind"] == kind and e.get("ask_id") == ask_id), None
        )
        if env is not None:
            got = _receive(w, label, env)
            outcome: Obj = (
                {"status": "answered", "reply": env["mail_id"]}
                if kind == "reply"
                else {"status": "member_ended", "result": env["result"]}
            )
            return close_ask(w, label, ask_id, outcome, text(got["event_id"]))
    if cancelled:
        return close_ask(w, label, ask_id, {"status": "cancelled"}, None)
    return close_ask(w, label, ask_id, {"status": "timed_out"}, None) if due else None


def _open_ask(w: World, label: str, ask_id: JsonValue) -> bool:
    branch = w.logs[label].branch
    return any(
        a["ask_id"] == ask_id and a["asker_branch_id"] == branch and a["state"] == "open"
        for a in w.rows("asks")
    )


def _settles(w: World, label: str) -> dict[str, str]:
    """Every settle MonitorId this log registered -> its WaitId."""
    log, out = w.logs[label], dict[str, str]()
    for e in log.events:
        d = obj(e["data"])
        if e["type"] == "wait_started":
            for m in arr(d["members"]):
                out[f"{log.branch}:{e['event_id']}:{text(obj(m)['name'])}"] = text(d["wait_id"])
    return out


def _waits(w: World, label: str) -> dict[str, str]:
    """This log's open waits: settle MonitorId -> WaitId."""
    finished = (e for e in w.logs[label].events if e["type"] == "wait_finished")
    done = {text(obj(e["data"])["wait_id"]) for e in finished}
    return {k: v for k, v in _settles(w, label).items() if v not in done}


def _committed(w: World, label: str, wait_id: str) -> None:
    """Every settle or end notice of this wait already committed counts: consume it before the
    wait finishes, whatever finishes it (commit order, never timestamps)."""
    monitors = {k for k, v in _waits(w, label).items() if v == wait_id}
    for env in _mine(w, label):
        if env["kind"] in NOTICES and env["monitor_id"] in monitors:
            _receive(w, label, env)


def _control(w: World, label: str, env: Obj) -> bool:
    """Control mail, the exhaustive list, applied now; False for ordinary mail."""
    kind = text(env["kind"])
    member_park: Obj = {"kind": "member", "id": env.get("monitor_id")}
    team_log = w.is_team_log(label)
    if kind == "cancel":
        apply_cancel(w, label, env)
    elif kind in ("reply", "bounce") and "ask_id" in env:
        if _open_ask(w, label, env["ask_id"]):
            complete(w, label, text(env["ask_id"]), cancelled=False, due=False)
        else:
            _receive(w, label, env)  # a late reply or ask bounce is recorded only
    elif kind in NOTICES and _notice(w, label, env):
        pass
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


def _notice(w: World, label: str, env: Obj) -> bool:
    """A settle or end notice that is control mail: its wait's (open, or finished and so
    recorded only), the team log's, or one resolving a member park."""
    monitor = text(env["monitor_id"])
    member_park: Obj = {"kind": "member", "id": monitor}
    team_log = w.is_team_log(label)
    if monitor in _waits(w, label):
        got = _receive(w, label, env)
        finish(w, label, _waits(w, label)[monitor], text(got["event_id"]))
    elif monitor in _settles(w, label):
        _receive(w, label, env)  # its wait already finished: recorded only
    elif team_log or member_park in arr(w.state(label)["parked"]):
        got = _receive(w, label, env)
        if not team_log:
            w.add(label, "resumed", {"address": member_park, "cause_event_id": got["event_id"]})
    else:
        return False
    return True


def apply_cancel(w: World, label: str, env: Obj) -> None:
    """Applying a cancel: its receipt and today's barrier, cancel_requested{scope: tree}. Every
    park it leaves is released in the same append: an open ask completes (a pending reply or
    bounce still wins, else cancelled), a wait finishes with what has settled, and a member park
    resumes. The barrier rules then end the member."""
    got = text(_receive(w, label, env)["event_id"])
    who = obj(obj(env["provenance"])["principal"])
    w.add(label, "cancel_requested", {"scope": "tree"}, who=who)
    for p in arr(w.state(label)["parked"]):
        address = obj(p)
        ident = text(address["id"])
        if address["kind"] == "ask" and _open_ask(w, label, ident):
            complete(w, label, ident, cancelled=True, due=False)
        elif address["kind"] == "wait" and ident in _waits(w, label).values():
            _committed(w, label, ident)
            finish(w, label, ident, got, deadline=True)
        elif address["kind"] == "member":
            w.add(label, "resumed", {"address": address, "cause_event_id": got})


def _pair(prov: JsonValue) -> tuple[str, str]:
    """(principal, root_request): one turn's authority and budget root."""
    p = obj(prov)
    root = obj(p["root_request"])
    return (principal(p["principal"]), f"{text(root['thread_id'])}:{text(root['event_id'])}")


def consume(w: World, label: str, _inp: Obj) -> Obj:
    """mail.consume: the recipient's pending rows in (created_at, mail_id) order. Mid-turn only
    rows of the open turn's (principal, root_request); else the longest prefix sharing the first
    row's pair. Ordinary mail a control row unblocks waits for the next consume."""
    row = w.own_row(label)
    rows = w.pending(TEAM_LOG if row is None else text(row["name"]))
    if not rows:
        return {"status": "nothing_pending"}
    if row is not None and row["state"] == "ended":
        return refuse_all(w, label, row)
    state = w.state(label)
    turn = _pair(w.turn_provenance(label)) if state["status"] == "in_turn" else None
    blocked = bool(arr(state["parked"]))
    batch: tuple[str, str] | None = None
    for m in rows:
        env = obj(m["envelope"])
        if not _still_pending(w, label, env["mail_id"]):
            continue  # an earlier control row of this pass took it
        if _control(w, label, env):
            blocked = blocked or env["kind"] == "cancel"  # a cancelled member takes no new work
            continue
        if blocked or arr(w.state(label)["parked"]):
            continue
        pair = _pair(env["provenance"])
        if turn is not None and pair != turn:
            continue
        if turn is None and batch is not None and pair != batch:
            blocked = True  # the longest prefix ends here
            continue
        batch = pair
        _receive(w, label, env)
    left = {text(e["mail_id"]) for e in _mine(w, label)}
    taken: list[JsonValue] = [m["mail_id"] for m in rows if m["mail_id"] not in left]
    return {"status": "consumed", "mail_ids": taken}


def deadline(w: World, label: str, inp: Obj) -> Obj:
    """The team worker's deadline step for one ask or wait of label, under its writer."""
    key = text(inp["id"])
    ask = next((a for a in w.rows("asks") if a["ask_id"] == key), None)
    if ask is not None:
        return _ask_due(w, label, key, ask)
    started = next(
        e
        for e in w.logs[label].events
        if e["type"] == "wait_started" and obj(e["data"])["wait_id"] == key
    )
    if w.now < num(obj(started["data"])["deadline"]) or key not in _waits(w, label).values():
        return {"status": "not_due"}
    cancel = next((e for e in _mine(w, label) if e["kind"] == "cancel"), None)
    if cancel is not None:
        apply_cancel(w, label, cancel)  # the barrier finishes the wait with what has settled
        return {"wait_id": key, "status": "cancelled"}
    _committed(w, label, key)
    return finish(w, label, key, None, deadline=True)


def _ask_due(w: World, label: str, key: str, ask: Obj) -> Obj:
    if ask["state"] != "open":
        return {"status": "not_due"}
    if w.is_team_log(label) and w.team()["closed_at"] is not None:
        # Team close is a trigger of its own: the team log's next step closes its open asks.
        return complete(w, label, key, cancelled=True, due=False) or {"status": "not_due"}
    if w.now < num(ask["deadline"]):
        return {"status": "not_due"}
    replied = any(
        e.get("ask_id") == key and e["kind"] in ("reply", "bounce") for e in _mine(w, label)
    )
    cancel = next((e for e in _mine(w, label) if e["kind"] == "cancel"), None)
    if cancel is not None and not replied:
        apply_cancel(w, label, cancel)  # the barrier closes the ask cancelled
        return {"ask_id": key, "status": "cancelled"}
    return complete(w, label, key, cancelled=False, due=True) or {"status": "not_due"}


def refuse_all(w: World, label: str, row: Obj) -> Obj:
    """Mail reaching a member that already ended is refused under its writer, never left pending:
    mail_refused{member_ended}, and a bounce only for a message or an ask, whose sender waits."""
    taken: list[JsonValue] = []
    for env in _mine(w, label):
        why = w.add(label, "mail_refused", {"mail_id": env["mail_id"], "code": "member_ended"})
        if env["kind"] in ("message", "ask"):
            bounce(w, label, env, why)
        taken.append(env["mail_id"])
    return {"status": "refused", "mail_ids": taken}
