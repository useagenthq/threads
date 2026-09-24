# pyright: strict
"""One team request in the reference executor: a model tool call (its pending tool_call) or an
operator request (an operator_request in the team log). It records the policy decision, the
refusal and the success the same way for every op (spec/schema/README.md, "Teams")."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .common import eid, num, obj, sha, text
from .jcs import canonical
from .ops_world import Refused
from .team_pieces import Route, at, envelope

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj
    from .ops_world import World


@dataclass(slots=True)
class Request:
    w: World
    label: str
    args: Obj
    key: Obj  # {call_id} or {request_id}: the request key of every decision it records
    sender: Obj  # the caller's MemberRef, or {operator: request_id}
    prov: Obj
    causal: Obj
    principal: Obj

    @property
    def operator(self) -> bool:
        return "request_id" in self.key

    @property
    def mail_id(self) -> str:
        """A call's or request's mail: <sender branch_id>:<call_id or request_id>."""
        return f"{self.w.logs[self.label].branch}:{text(next(iter(self.key.values())))}"

    def decide(self, op: str, target: str, allow: bool) -> None:
        """Phase 1 policy: the team's grant (source team), else default deny."""
        decision: Obj = {
            "op": op,
            "decision": "allow" if allow else "deny",
            "source": "team" if allow else "default",
            "target": target,
            **self.key,
        }
        self.w.add(self.label, "message_policy_decided", decision)
        if not allow:
            raise Refused("forbidden")

    def refuse(self, code: str, detail: Obj | None = None) -> Obj:
        value: Obj = {"code": code, "status": "refused"}
        if detail is not None:
            value["detail"] = detail
        if self.operator:
            data: Obj = {"request_id": self.key["request_id"], "code": code}
            self.w.add(self.label, "operator_refused", data)
        else:
            self.w.answer(self.label, text(self.key["call_id"]), value)
        return value

    def done(self, value: Obj) -> Obj:
        if not self.operator:
            self.w.answer(self.label, text(self.key["call_id"]), value)
        return value

    def target(self, to: JsonValue) -> Obj:
        """The addressed member's current row. A model names a member and gets the generation
        its own log last recorded; an operator names a MemberRef."""
        if self.operator:
            ref = obj(to)
            name, generation = text(ref["name"]), num(ref["generation"])
            if ref["team"] != self.w.team()["team_id"]:
                raise Refused("unknown_member")
        else:
            name, generation = text(to), self.w.bound(self.label, text(to)) or 0
        row = self.w.member(name)
        if row is None or generation > num(row["generation"]):
            raise Refused("unknown_member")
        if generation < num(row["generation"]):
            raise Refused("stale_member")
        return row

    def is_self(self, row: Obj) -> bool:
        return self.sender.get("name") == row["name"]


def open_request(w: World, label: str, op: str, inp: Obj) -> Request:
    """A model call names its pending tool_call; an operator request appends operator_request,
    whose own event is its provenance's root request (and inserts the receipt row with a key)."""
    log = w.logs[label]
    if "call_id" in inp:
        cid = text(inp["call_id"])
        c = w.call(label, cid, obj(inp["args"]))
        prov = w.turn_provenance(label)
        return Request(
            w,
            label,
            obj(inp["args"]),
            {"call_id": cid},
            w.caller(label),
            prov,
            at(text(c["event_id"]), log.thread),
            obj(prov["principal"]),
        )
    rid, who, body = text(inp["request_id"]), obj(inp["principal"]), obj(inp["body"])
    root: Obj = {"thread_id": log.thread, "event_id": _next_event_id(w, label)}
    op_prov: Obj = {"principal": who, "root_request": root, "via": []}
    data: Obj = {
        "request_id": rid,
        "op": op,
        "principal": who,
        "body_hash": sha(canonical(body)),
        "provenance": op_prov,
    }
    if "idempotency_key" in inp:
        data["idempotency_key"] = inp["idempotency_key"]
    e = w.add(label, "operator_request", data, who=who)
    return Request(
        w,
        label,
        body,
        {"request_id": rid},
        {"operator": rid},
        op_prov,
        at(text(e["event_id"]), log.thread),
        who,
    )


def _next_event_id(w: World, label: str) -> str:
    log = w.logs[label]
    return eid(log.seq + 1, log.branch)


def same_tenant(req: Request) -> bool:
    """An operator acting for a principal of another tenant is forbidden."""
    return req.principal["tenant"] == req.w.team()["tenant_id"]


def park(w: World, label: str, address: Obj) -> None:
    """A member parks on its ask or wait. Its first park while its task monitor is unfired also
    sends the one non-consuming member_parked notice to the starter."""
    log = w.logs[label]
    first = not any(e["type"] == "parked" for e in log.events)
    parked = w.add(label, "parked", {"address": address, "reason": "awaiting_member"})
    row = w.own_row(label)
    task = next(
        (
            m
            for m in w.rows("monitors")
            if row and m["kind"] == "task" and m["target_name"] == row["name"]
        ),
        None,
    )
    if not first or row is None or task is None:
        return
    notice = envelope(
        f"{log.branch}:{eid(log.seq + 1, log.branch)}",
        "member_parked",
        Route(w.ref(row), w.address(text(task["watcher_branch_id"])), w.turn_provenance(label)),
        at(text(parked["event_id"]), log.thread),
        monitor_id=task["monitor_id"],
        reason="awaiting_member",
    )
    w.add(label, "message_sent", {"envelope": notice})
