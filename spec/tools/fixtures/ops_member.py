# pyright: strict
"""Reference member.start (spec/schema/README.md, "Teams"; design §4.10): the starter's
member_started and task mail, which insert the `starting` row, the pending task and the starter's
task monitor. The member's branch opens later, at materialize (ops_start.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import eid, obj, text
from .dynamic_rules import resolve
from .ops_request import keyed, open_request, same_tenant
from .ops_world import Refused
from .team_pieces import Route, body, config_hash, envelope

if TYPE_CHECKING:
    from .jcs import Obj
    from .ops_request import Request
    from .ops_world import World

LIVE = ("starting", "running")


def _checks(req: Request, agent: str) -> Obj:
    """Policy (only a lead, or the operator, starts), team open, the agent listed, the start's
    label and chosen fields (resolved against a dynamic agent's template), the concurrent cap
    (starting and running members count), then budget headroom. Returns the resolution."""
    w = req.w
    caller = None if req.operator else w.own_row(req.label)
    grant = same_tenant(req) if req.operator else caller is not None and caller["role"] == "lead"
    req.decide("start", agent, allow=grant)
    if w.team()["closed_at"] is not None:
        raise Refused("team_closed")
    if agent not in w.agents and agent not in w.templates:
        raise Refused("unknown_agent")
    resolved = resolve(w.templates.get(agent), req.args)
    if "error" in resolved:
        raise Refused("invalid_definition", obj(resolved["error"]))
    rows = w.rows("team_members")
    if sum(r["role"] == "member" and r["state"] in LIVE for r in rows) >= w.concurrent:
        raise Refused("concurrency_cap")
    if not w.headroom:
        raise Refused("budget_exceeded")
    return obj(resolved["ok"])


def _parent(w: World, req: Request, started_id: str) -> Obj:
    """Through the lead: a model start names its own member_started; an operator start names
    the lead's thread_started, which carries the team."""
    lead = next(r for r in w.rows("team_members") if r["role"] == "lead")
    log = w.logs[w.label_of(text(lead["branch_id"]))]
    event = text(log.events[0]["event_id"]) if req.operator else started_id
    own = w.logs[req.label] if not req.operator else log
    return {
        "thread_id": own.thread,
        "branch_id": own.branch,
        "event_id": event,
        "relation": "team_member",
    }


def start(w: World, label: str, inp: Obj) -> Obj:
    replayed = keyed(w, label, "start", inp)
    if replayed is not None:
        return replayed
    req = open_request(w, label, "start", inp)
    agent = text(req.args["agent"])
    try:
        resolved = _checks(req, agent)
    except Refused as r:
        return req.refuse(r.code, r.detail)
    k = 1 + sum(r["agent"] == agent and r["role"] == "member" for r in w.rows("team_members"))
    member: Obj = {
        "tenant": w.team()["tenant_id"],
        "team": w.team()["team_id"],
        "name": f"{agent}-{k}",
        "generation": 1,
    }
    log = w.logs[label]
    started_id = eid(log.seq + 1, log.branch)
    data: Obj = {
        "member": member,
        "agent": agent,
        # A dynamic member's pin is its template's with the define; the vector gives its hash.
        "config_hash": text(inp.get("config_hash", config_hash(agent))),
        "thread_id": inp["thread_id"],
        "parent": _parent(w, req, started_id),
        "provenance": req.prov,
        **resolved,
    }
    w.add(label, "member_started", data)
    task = envelope(
        req.mail_id,
        "task",
        Route(req.sender, text(member["name"]), req.prov),
        req.causal,
        body=body(text(req.args["task"])),
    )
    w.add(label, "message_sent", {"envelope": task})
    return req.done({"member": member, "status": "started"})
