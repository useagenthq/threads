# pyright: strict
"""Team op vectors of Teams Phase 2 (spec/schema/README.md, "Teams Phase 2"): a caller's ask and
send of a host member under the host rules, a host member's reply to a caller, the caller's
consume, a host member's failed and hop-capped turns, the supervisor step, and deleting a caller.
Each names the sub-lane that builds it (`lane`), and runtimes skip it until then."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import ops_host
from .common import obj, text
from .host_cases import asked, close_ask, replied
from .host_pieces import (
    BILLING_BRANCHES,
    POLICY,
    SUPPORT_BRANCH,
    billing,
    billing_log,
    caller_log,
    host_start,
    host_team_log,
    take,
    turn_failed,
)
from .ops_world import World
from .pieces import call, user
from .team_ops_worlds import DUE, NOW, Vec

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj
    from .log import Log

P, S, R = "message_policy_decided", "message_sent", "tool_result"
ASK_ONLY: list[JsonValue] = [{"from": "support", "to": "billing", "allow": ["ask"]}]
ASK_ID = f"{SUPPORT_BRANCH}:c1"
ERROR: Obj = {"code": "content_unsupported", "message": "billing's turn failed"}
BUDGET_ERROR: Obj = {"code": "budget_exhausted", "message": "the hop cap ended billing's turn"}
QUESTION: Obj = {"to": "billing", "question": "Is INV-1001 paid?"}


def _world(**logs: Log) -> World:
    return World(dict(logs), NOW)


def _caller_world(tool: str, args: Obj) -> World:
    """support's model has asked for the tool: the world before the call's decision."""
    support = caller_log()
    user(support, "Is INV-1001 paid?")
    call(support, tool, args, "c1")
    return _world(team=host_team_log(), billing=billing_log(), support=support)


def _vec(  # noqa: PLR0913, PLR0917 - Vec's own fields, positional as its other builders pass them
    name: str,
    desc: str,
    w: World,
    op: str,
    by: str,
    inp: Obj,
    outcome: Obj,
    appended: dict[str, list[str]],
    lane: str = "29D",
) -> Vec:
    return Vec(
        name, "29", desc, w, op, by, inp, outcome, appended, given={"rules": ASK_ONLY}, lane=lane
    )


def host_vectors() -> list[Vec]:
    return [*_calls(), *_answers(), *_failures(), *_supervision(), *_deletion()]


def _calls() -> list[Vec]:
    ask_in: Obj = {"call_id": "c1", "args": QUESTION}
    send_args: Obj = {"to": "billing", "text": "FYI: INV-1001."}
    return [
        _vec(
            "host-caller-ask-opens",
            "support, a caller in no team, asks billing, a host member. The host rule "
            "{from: support, to: billing, allow: [ask]} decides it (source message_policy); the "
            "ask is from the caller address to billing's current generation, and support parks. "
            "The mail row is to_kind member in the host team; the asks row names support's "
            "branch as asker.",
            _caller_world("ask", QUESTION),
            "ask",
            "support",
            ask_in,
            {"status": "open", "ask_id": ASK_ID, "deadline": DUE},
            {"support": [P, S, "parked"]},
        ),
        _vec(
            "host-caller-send-refused-by-rule",
            "support's rule allows only ask, so its send to billing is decided by default deny "
            "(source default): the call's result is refused forbidden, and nothing is sent.",
            _caller_world("send", send_args),
            "send",
            "support",
            {"call_id": "c1", "args": send_args},
            {"code": "forbidden", "status": "refused"},
            {"support": [P, R]},
        ),
    ]


def _answers() -> list[Vec]:
    team, bill, support, ask = asked()
    take(bill, ask)
    reply_args: Obj = {"ask_id": ASK_ID, "text": "Paid."}
    call(bill, "reply", reply_args, "r1")
    t2, b2, s2, _ask, _back = replied()
    return [
        _vec(
            "host-member-reply-to-caller",
            "billing replies to support's ask: the reply goes to the caller address the ask "
            "came from, with the ask's provenance, and inserts a to_kind caller mail row whose "
            "to_branch_id is support's branch. No policy decision: only the asked member replies.",
            _world(team=team, billing=bill, support=support),
            "reply",
            "billing",
            {"call_id": "r1", "args": reply_args},
            {"id": f"{BILLING_BRANCHES[1]}:r1", "status": "sent"},
            {"billing": [S, R]},
        ),
        _vec(
            "host-caller-consumes-reply",
            "support consumes billing's reply from the caller index: message_received, "
            "ask_closed answered, resumed and the ask call's one result.",
            _world(team=t2, billing=b2, support=s2),
            "consume",
            "support",
            {},
            {"status": "consumed", "mail_ids": [f"{BILLING_BRANCHES[1]}:r1"]},
            {"support": ["message_received", "ask_closed", "resumed", R]},
        ),
    ]


def _taken() -> World:
    team, bill, support, ask = asked()
    take(bill, ask)
    return _world(team=team, billing=bill, support=support)


def _failures() -> list[Vec]:
    team, bill, support, ask = asked()
    take(bill, ask)
    end = bill.add("turn_completed", {"reason": "error", "code": "content_unsupported"})
    turn_failed(bill, [ask], ERROR, end)
    hop: Obj = {"limit": "max_cost_nanos", "limit_value": 500_000_000, "observed": 600_000_000}
    return [
        _vec(
            "host-turn-failure-ends-only-the-turn",
            "billing's turn, holding support's ask, fails before dispatch "
            "(turn_completed{error}). Only the turn ends: the ask bounces turn_failed with the "
            "error to the caller, and member_idle{turn_failed} puts billing back to idle. No "
            "member_ended, no supervisor.",
            _taken(),
            "turn_failure",
            "billing",
            {"turn": {"reason": "error", "code": "content_unsupported"}, "error": ERROR},
            {"status": "idle", "failed": [ASK_ID]},
            {"billing": ["turn_completed", S, "member_idle"]},
        ),
        _vec(
            "host-hop-cap-ends-only-the-turn",
            "The rule's per-hop cap refuses billing's next reservation: budget_exceeded{scope: "
            "hop}, turn_completed{budget_exhausted}, the ask's turn_failed bounce with code "
            "budget_exhausted, member_idle{turn_failed}.",
            _taken(),
            "turn_failure",
            "billing",
            {"hop": hop, "turn": {"reason": "budget_exhausted"}, "error": BUDGET_ERROR},
            {"status": "idle", "failed": [ASK_ID]},
            {"billing": ["budget_exceeded", "turn_completed", S, "member_idle"]},
        ),
        _vec(
            "host-caller-consumes-turn-failed",
            "support consumes billing's turn_failed bounce: the ask closes failed with the "
            "bounce's error (asks.state failed), and the ask call's result is AskOutcome.failed.",
            _world(team=team, billing=bill, support=support),
            "consume",
            "support",
            {},
            {"status": "consumed", "mail_ids": [_last_mail(bill)]},
            {"support": ["message_received", "ask_closed", "resumed", R]},
        ),
    ]


def _last_mail(log: Log) -> str:
    e = next(e for e in reversed(log.events) if e["type"] == "message_sent")
    return text(obj(obj(e["data"])["envelope"])["mail_id"])


def _ended(bill: Log, status: str = "failed") -> None:
    result: Obj = {"member": billing(1), "status": status}
    if status == "failed":
        result["error"] = {"code": "pin_unavailable", "message": "rebind failed: pin_unavailable"}
    bill.add("member_ended", {"result": result})


def _supervision() -> list[Vec]:
    team, bill = host_team_log(), billing_log(1)
    _ended(bill)
    decided_team = host_team_log()
    decided_bill = billing_log(1)
    _ended(decided_bill)
    end = decided_bill.events[-1]
    data: Obj = {
        "member": billing(1),
        "ended": {"branch_id": end["branch_id"], "seq": end["seq"]},
        "action": "restart",
        "restarts_in_window": 0,
        "policy": {**POLICY, "max_restarts": 1, "within_ms": 600_000},
    }
    decided_team.add("supervisor_decided", data)
    decided_team.add("member_started", host_start(2, restart_of=1))
    bill2 = billing_log(2)
    bill2.add(
        "member_ended",
        {
            "result": {
                "member": billing(2),
                "status": "failed",
                "error": {"code": "pin_mismatch", "message": "rebind failed: pin_mismatch"},
            }
        },
    )
    stopped_team, stopped_bill = host_team_log(), billing_log(1)
    _ended(stopped_bill)
    stop = stopped_bill.events[-1]
    stopped_team.add(
        "supervisor_decided",
        {
            **data,
            "ended": {"branch_id": stop["branch_id"], "seq": stop["seq"]},
            "action": "stop",
            "policy": {**POLICY, "restart": "never"},
        },
    )
    cancelled_team, cancelled_bill = host_team_log(), billing_log(1)
    _ended(cancelled_bill, "cancelled")
    capped: Obj = {
        "member": "billing",
        "policy": {**POLICY, "max_restarts": 1, "within_ms": 600_000},
    }
    return [
        _vec(
            "host-supervise-restart",
            "billing's generation 1 ended failed (a rebind failure). The host team log's writer "
            "decides once: supervisor_decided{restart, restarts_in_window 0} and generation 2's "
            "member_started{restart_of: 1}, whose row starts.",
            _world(team=team, billing=bill),
            "supervise",
            "team",
            {"member": "billing", "policy": POLICY},
            {"status": "decided", "action": "restart", "restarts_in_window": 0},
            {"team": ["supervisor_decided", "member_started"]},
            lane="29E",
        ),
        _vec(
            "host-supervise-stop-at-cap",
            "With maxRestarts 1, generation 2 fails within the window of generation 1's restart: "
            "restarts_in_window 1, so the decision is stop, and no generation 3 starts.",
            _world(team=decided_team, billing=decided_bill, billing2=bill2),
            "supervise",
            "team",
            capped,
            {"status": "decided", "action": "stop", "restarts_in_window": 1},
            {"team": ["supervisor_decided"]},
            lane="29E",
        ),
        _vec(
            "host-supervise-cancel-stops",
            "An operator's cancel ended billing cancelled: a cancel is a stop, never restarted.",
            _world(team=cancelled_team, billing=cancelled_bill),
            "supervise",
            "team",
            {"member": "billing", "policy": POLICY},
            {"status": "decided", "action": "stop", "restarts_in_window": 0},
            {"team": ["supervisor_decided"]},
            lane="29E",
        ),
        _vec(
            "host-supervise-already-decided",
            "With restart never, a first host decided stop on billing's end. A second host sees "
            "the same end: one decision per ended generation, so it appends nothing.",
            _world(team=stopped_team, billing=stopped_bill),
            "supervise",
            "team",
            {"member": "billing", "policy": POLICY},
            {"status": "already_decided"},
            {},
            lane="29E",
        ),
    ]


def _sent_world() -> World:
    """support sent billing a message, under a rule that allows send; billing hasn't taken it."""
    args: Obj = {"to": "billing", "text": "FYI: INV-1001."}
    w = _caller_world("send", args)
    w.stamp = False
    w.rules = [{"from": "support", "to": "billing", "allow": ["send"]}]
    ops_host.send(w, "support", {"call_id": "c1", "args": args})
    return _world(**w.logs)


def _deletion() -> list[Vec]:
    team, bill, support, _ask = asked()
    t2, b2, s2, _a2, _back2 = replied()
    t3, b3, s3, _a3, back3 = replied()
    answered: Obj = {"status": "answered", "member": billing(), "text": "Paid."}
    close_ask(s3, back3, {"status": "answered", "reply": back3["mail_id"]}, answered)
    return [
        _vec(
            "delete-caller-busy-open-ask",
            "Deleting support while its ask of billing is open is busy, and nothing is written.",
            _world(team=team, billing=bill, support=support),
            "delete",
            "support",
            {},
            {"status": "refused", "code": "busy"},
            {},
        ),
        _vec(
            "delete-caller-busy-pending-send",
            "Deleting support while its message to billing is still pending is busy: the delete "
            "finds the caller's sent mail by the mail ids of its own log's message_sent.",
            _sent_world(),
            "delete",
            "support",
            {},
            {"status": "refused", "code": "busy"},
            {},
        ),
        _vec(
            "delete-caller-busy-unconsumed-reply",
            "Deleting support while billing's reply to it is unconsumed is busy.",
            _world(team=t2, billing=b2, support=s2),
            "delete",
            "support",
            {},
            {"status": "refused", "code": "busy"},
            {},
        ),
        _vec(
            "delete-caller-quiescent",
            "Once support has consumed billing's reply, deleting it removes every host team "
            "mail and asks row naming its branch, and a rebuild skips them too.",
            _world(team=t3, billing=b3, support=s3),
            "delete",
            "support",
            {},
            {"status": "deleted"},
            {},
        ),
    ]
