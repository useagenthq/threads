# pyright: strict
"""Building blocks of the team wire vector: one canonical event line, and the envelopes and
results its cases share."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, T0, aref
from .jcs import canonical
from .team_pieces import (
    DEADLINE,
    LEAD,
    LEAD_BRANCH,
    LEAD_THREAD,
    MEMBER_THREAD,
    REQUEST,
    RESEARCHER,
    Route,
    at,
    body,
    completed,
    envelope,
    provenance,
)

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

E1 = "0192e000-0000-7000-8000-000000000001"
E2 = "0192e000-0000-7000-8000-000000000002"
HOST: Obj = {"kind": "host", "principal": ALICE}
PROV = provenance(E1)
FROM_OP: Obj = {"operator": REQUEST}
MONITOR = f"{LEAD_BRANCH}:{E2}:task"
BUDGET: Obj = {
    "scope": "thread",
    "limit": "max_turns",
    "limit_value": 3,
    "observed": 3,
    "observed_is_upper_bound": False,
}
PARENT: Obj = {
    "thread_id": LEAD_THREAD,
    "branch_id": LEAD_BRANCH,
    "event_id": E2,
    "relation": "team_member",
}


def line(type_: str, data: Obj, actor: Obj | None = None) -> str:
    e: Obj = {
        "seq": 2,
        "event_id": E2,
        "thread_id": LEAD_THREAD,
        "branch_id": LEAD_BRANCH,
        "epoch": 1,
        "type": type_,
        "type_version": 1,
        "time": T0,
        "actor": actor or {"kind": "host"},
        "prev_hash": "0" * 64,
        "critical": True,
        "data": data,
    }
    return canonical(e).decode()


def mail(kind: str, sender: Obj = LEAD, to: str = "researcher-1", **k: JsonValue) -> Obj:
    return envelope(f"{LEAD_BRANCH}:c1", kind, Route(sender, to, PROV), at(E1), **k)


def sent(env: Obj) -> str:
    return line("message_sent", {"envelope": env})


TASK = mail("task", body=body("Topic: batteries."))
ASK = mail("ask", ask_id=f"{LEAD_BRANCH}:c1", deadline=DEADLINE, body=body("Which topic?"))
SETTLED = mail(
    "member_settled",
    RESEARCHER,
    "lead",
    monitor_id=MONITOR,
    result=completed(RESEARCHER, "Prices fell."),
)
STARTED: Obj = {
    "member": RESEARCHER,
    "agent": "researcher",
    "config_hash": "a" * 64,
    "thread_id": MEMBER_THREAD,
    "parent": PARENT,
    "provenance": PROV,
}
POLICY: Obj = {"op": "send", "decision": "allow", "source": "team", "target": "researcher-1"}
BIG = aref(b"A long answer.", "text/plain")
