# pyright: strict
"""Reference ops that watch members (spec/schema/README.md, "Teams", "Waits and monitors"; design
§4.12 and §4.13): wait and monitor, and how a wait finishes. A settled target is observed in the
registering append; any other gets a monitor row, which its idle or end append fires."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, num, obj, text
from .ops_request import Request, open_request, park, same_tenant
from .ops_world import DEFAULT_MS, Refused, public

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj
    from .ops_world import World

SETTLED = ("idle", "ended")


def _satisfied(mode: JsonValue, settled: int, total: int) -> bool:
    need = total if mode == "all" else 1 if mode == "any" else num(mode)
    return settled >= need


def _observe(req: Request, monitor_id: str, row: Obj) -> Obj:
    """member_observed: the target's committed result, copied from where it is."""
    seen = req.w.settled_at(row)
    req.w.add(req.label, "member_observed", {"monitor_id": monitor_id, **seen})
    return obj(seen["result"])


def _members(req: Request) -> list[Obj]:
    """The listed members as rows, a repeated one dropped; each known at its generation."""
    listed = [obj(m) if req.operator else m for m in arr(req.args["members"])]
    unique = [m for i, m in enumerate(listed) if m not in listed[:i]]
    return [req.target(m) for m in unique]


def wait(w: World, label: str, inp: Obj) -> Obj:
    # The model's wait tool has no mode and no timeout: always all, with the default deadline.
    mode: JsonValue = obj(inp["body"]).get("mode", "all") if "body" in inp else "all"
    if "body" in inp:
        refs = arr(obj(inp["body"])["members"])
        unique = [m for i, m in enumerate(refs) if m not in refs[:i]]
        if isinstance(mode, int) and mode > len(unique):
            return {"code": "invalid_request", "status": "refused"}  # before anything is recorded
    req = open_request(w, label, "wait", inp)
    try:
        rows = _members(req)
        for row in rows:
            req.decide("monitor", text(row["name"]), allow=not req.operator or same_tenant(req))
    except Refused as r:
        return req.refuse(r.code)
    asked = num(req.args.get("timeout_ms", DEFAULT_MS)) if req.operator else DEFAULT_MS
    timeout = min(asked, DEFAULT_MS)
    wait_id = req.mail_id
    refs: list[JsonValue] = [w.ref(r) for r in rows]
    started = w.add(
        label,
        "wait_started",
        {"wait_id": wait_id, "members": refs, "mode": mode, "deadline": w.now + timeout},
    )
    settled = 0
    for row in rows:
        if row["state"] in SETTLED:
            _observe(req, f"{w.logs[label].branch}:{started['event_id']}:{row['name']}", row)
            settled += 1
    if _satisfied(mode, settled, len(rows)):
        return finish(w, label, wait_id, cause=None)
    if not req.operator:
        park(w, label, {"kind": "wait", "id": wait_id})
    return {"status": "waiting", "wait_id": wait_id}


def finish(w: World, label: str, wait_id: str, cause: str | None, deadline: bool = False) -> Obj:
    """wait_finished from this log's evidence (member_observed and received notifications), in
    the wait's member order; a member waiter also resumes and records the call's one result."""
    log = w.logs[label]
    start = next(
        e
        for e in log.events
        if e["type"] == "wait_started" and obj(e["data"])["wait_id"] == wait_id
    )
    d = obj(start["data"])
    evidence: dict[str, JsonValue] = {}
    for e in log.events:
        data = obj(e["data"])
        if e["type"] == "member_observed":
            evidence[text(data["monitor_id"])] = data["result"]
        elif e["type"] == "message_received" and "monitor_id" in obj(data["envelope"]):
            env = obj(data["envelope"])
            evidence[text(env["monitor_id"])] = env["result"]
    finished: list[JsonValue] = []
    parked: list[JsonValue] = []
    pending: list[JsonValue] = []
    for m in arr(d["members"]):
        ref = obj(m)
        got = evidence.get(f"{log.branch}:{start['event_id']}:{ref['name']}")
        row = w.member(text(ref["name"]))
        if got is not None:
            finished.append(got)
        elif row is not None and row["state"] == "parked":
            parked.append({"member": ref, "reason": w.park_reason(row)})
        else:
            pending.append(ref)
    met = _satisfied(d["mode"], len(finished), len(arr(d["members"])))
    if not met and not deadline:
        return {"status": "waiting", "wait_id": wait_id}
    done: Obj = {
        "wait_id": wait_id,
        "finished": finished,
        "parked": parked,
        "pending": pending,
        "timed_out": not met,
    }
    e = w.add(label, "wait_finished", done)
    waited: Obj = {
        "status": "waited",
        "finished": [public(r) for r in finished],
        "parked": parked,
        "pending": pending,
        "timed_out": not met,
    }
    if not w.is_team_log(label):
        address: Obj = {"kind": "wait", "id": wait_id}
        if address in arr(w.state(label)["parked"]):
            w.add(label, "resumed", {"address": address, "cause_event_id": cause or e["event_id"]})
        w.answer(label, call_of(wait_id), waited)
    return waited


def call_of(key: str) -> str:
    """A wait's or ask's call: the id after its sender branch."""
    return key.split(":", 1)[1]


def monitor(w: World, label: str, inp: Obj) -> Obj:
    req = open_request(w, label, "monitor", inp)
    name = text(req.args["member"])
    try:
        req.decide("monitor", name, allow=True)
        row = req.target(name)
    except Refused as r:
        return req.refuse(r.code)
    ref = w.ref(row)
    set_ = w.add(label, "monitor_set", {"member": ref})
    if row["state"] == "ended":
        result = _observe(req, f"{w.logs[label].branch}:{set_['event_id']}:{name}", row)
        return req.done({"result": public(result), "status": "ended"})
    return req.done({"member": ref, "status": "monitoring"})
