# pyright: strict
"""A staged `team` case for the operator side (spec/schema/README.md, "Teams"): team.start and
team.wait as operator requests in the team log, which takes member_started and member_observed."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, eid, sha, text
from .jcs import canonical
from .team_pieces import (
    DEADLINE,
    LEAD_BRANCH,
    LEAD_THREAD,
    LOG_BRANCH,
    LOG_THREAD,
    MEMBER_BRANCH,
    MEMBER_THREAD,
    RESEARCHER,
    Route,
    at,
    body,
    config_hash,
    envelope,
    lead_log,
    team_log,
)
from .team_steps import host, idle, materialize, received, write_team

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj
    from .log import Log

REQUESTS = ("0192d000-0000-7000-8000-000000000001", "0192d000-0000-7000-8000-000000000002")


def _request(log: Log, request_id: str, op: str, request_body: Obj, key: str) -> Obj:
    """operator_request, whose own event is its provenance's root request, then the team's
    grant for it (wait is decided as monitor)."""
    this = eid(log.seq + 1, LOG_BRANCH)
    prov: Obj = {
        "principal": ALICE,
        "root_request": {"thread_id": LOG_THREAD, "event_id": this},
        "via": [],
    }
    host(
        log,
        "operator_request",
        {
            "request_id": request_id,
            "op": op,
            "principal": ALICE,
            "idempotency_key": key,
            "body_hash": sha(canonical(request_body)),
            "provenance": prov,
        },
    )
    decision: Obj = {"op": "monitor" if op == "wait" else op, "decision": "allow", "source": "team"}
    log.add(
        "message_policy_decided",
        {
            **decision,
            "target": "researcher-1" if op == "wait" else "researcher",
            "request_id": request_id,
        },
    )
    return prov


def build(root: pathlib.Path) -> None:
    lead = lead_log()  # the lead has no run of its own: operator work never extends one
    ops = team_log()
    prov = _request(
        ops, REQUESTS[0], "start", {"agent": "researcher", "task": "Topic: batteries."}, "start-1"
    )
    lead_started = text(lead.events[0]["event_id"])
    started_id = eid(ops.seq + 1, LOG_BRANCH)
    parent: Obj = {
        "thread_id": LEAD_THREAD,
        "branch_id": LEAD_BRANCH,
        "event_id": lead_started,
        "relation": "team_member",
    }
    ops.add(
        "member_started",
        {
            "member": RESEARCHER,
            "agent": "researcher",
            "config_hash": config_hash("researcher"),
            "thread_id": MEMBER_THREAD,
            "parent": parent,
            "provenance": prov,
        },
    )
    route = Route({"operator": REQUESTS[0]}, "researcher-1", prov)
    request_event = (
        text(prov["root_request"]["event_id"]) if isinstance(prov["root_request"], dict) else ""
    )
    task = envelope(
        f"{LOG_BRANCH}:{REQUESTS[0]}",
        "task",
        route,
        at(request_event, LOG_THREAD),
        body=body("Topic: batteries."),
    )
    ops.add("message_sent", {"envelope": task})

    member = materialize(lead_started, task)
    done = idle(member, RESEARCHER, "Battery prices fell.")
    idle_seq = member.seq
    settled = envelope(
        f"{MEMBER_BRANCH}:{eid(member.seq + 1, MEMBER_BRANCH)}",
        "member_settled",
        Route(RESEARCHER, "team_log", prov),
        at(text(member.events[-1]["event_id"]), MEMBER_THREAD),
        monitor_id=f"{LOG_BRANCH}:{started_id}:task",
        result=done,
    )
    member.add("message_sent", {"envelope": settled})
    received(ops, settled)  # the team log records it: no park, no resume, no turn

    _request(ops, REQUESTS[1], "wait", {"members": [RESEARCHER]}, "wait-1")
    wait_id = f"{LOG_BRANCH}:{REQUESTS[1]}"
    wait = ops.add(
        "wait_started",
        {"wait_id": wait_id, "members": [RESEARCHER], "mode": "all", "deadline": DEADLINE},
    )
    source: Obj = {"thread_id": MEMBER_THREAD, "branch_id": MEMBER_BRANCH, "seq": idle_seq}
    ops.add(
        "member_observed",
        {
            "monitor_id": f"{LOG_BRANCH}:{wait['event_id']}:researcher-1",
            "result": done,
            "source": source,
        },
    )
    ops.add(
        "wait_finished",
        {"wait_id": wait_id, "finished": [done], "parked": [], "pending": [], "timed_out": False},
    )
    write_team(
        root,
        "team-operator-start-and-wait",
        "The operator's team.start is an operator_request in the team log, with the team's "
        "grant, member_started (its parent is the lead's thread_started) and the task mail "
        "from {operator: request_id}. The member settles; the team log records the task "
        "notification only. team.wait then finds the member idle and records member_observed "
        "and wait_finished in the same append. The index rebuilds both operator_receipts rows, "
        "the member idle, the task and notification consumed, and no monitor left.",
        {"lead": lead, "researcher": member, "team": ops},
    )
