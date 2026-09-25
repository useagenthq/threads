# pyright: strict
"""The staged Teams Phase 2 rejections (spec/schema/README.md, semantic rules 50-55): each log
breaks one rule at one event, which the reference validator (ref_host.py) catches there."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import eid, num, obj
from .host_cases import asked, write_host
from .host_pieces import (
    HOST_TEAM,
    POLICY,
    billing,
    billing_log,
    caller,
    host_start,
    host_team_log,
    take,
    turn_failed,
)
from .team_pieces import team_log
from .team_steps import received

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj
    from .log import Log

ERROR: Obj = {"code": "content_unsupported", "message": "billing's turn failed"}


def reject_at(
    root: pathlib.Path, name: str, desc: str, logs: dict[str, Log], at: tuple[str, Obj]
) -> None:
    label, e = at
    error: Obj = {"code": "invalid_transition", "seq": num(e["seq"]), "log": label}
    write_host(root, name, desc, logs, error)


def _ended(bill: Log) -> Obj:
    failed: Obj = {
        "member": billing(1),
        "status": "failed",
        "error": {"code": "pin_unavailable", "message": "rebind failed: pin_unavailable"},
    }
    return bill.add("member_ended", {"result": failed})


def _decision(ended: Obj, action: str, count: int) -> Obj:
    return {
        "member": billing(1),
        "ended": {"branch_id": ended["branch_id"], "seq": ended["seq"]},
        "action": action,
        "restarts_in_window": count,
        "policy": POLICY,
    }


def build(root: pathlib.Path) -> None:
    _placement(root)
    _supervision(root)
    _callers(root)
    _turn_failures(root)
    _classes(root)


def _placement(root: pathlib.Path) -> None:
    lead_team = team_log()
    bad = lead_team.add("member_started", host_start(1))
    reject_at(
        root,
        "host-member-started-in-lead-team-rejected",
        "Rule 50: only a host team's log starts host members: a lead's team log records "
        "member_started{host_member}: invalid_transition there, in the team log.",
        {"team": lead_team},
        ("team", bad),
    )
    lead_team, bill = team_log(), billing_log()
    bad = lead_team.add("supervisor_decided", _decision(_ended(bill), "restart", 0))
    reject_at(
        root,
        "supervisor-decided-in-lead-team-rejected",
        "Rule 50: supervisor_decided belongs to a host team's log only; a lead's team log takes "
        "none: invalid_transition there, in the team log.",
        {"team": lead_team, "billing": bill},
        ("team", bad),
    )


def _supervision(root: pathlib.Path) -> None:
    team, bill = host_team_log(), billing_log()
    ended = _ended(bill)
    team.add("supervisor_decided", _decision(ended, "stop", 0))
    bad = team.add("supervisor_decided", _decision(ended, "stop", 0))
    reject_at(
        root,
        "supervisor-decided-twice-rejected",
        "Rule 51: one supervisor_decided per ended generation. A second decision on billing's "
        "generation 1 (two hosts racing, the loser not rolled back) is invalid_transition.",
        {"team": team, "billing": bill},
        ("team", bad),
    )
    team, bill = host_team_log(), billing_log()
    bad = team.add("supervisor_decided", _decision(_ended(bill), "restart", 1))
    reject_at(
        root,
        "supervisor-restarts-miscounted-rejected",
        "Rule 51: restarts_in_window is the log's count of earlier restart decisions for the "
        "name within the window. The first decision records 1 where the log holds none: "
        "invalid_transition.",
        {"team": team, "billing": bill},
        ("team", bad),
    )
    team = host_team_log()
    bad = team.add("member_started", host_start(2, restart_of=1))
    reject_at(
        root,
        "host-restart-without-decision-rejected",
        "Rule 51: a restart (member_started{restart_of}) follows the supervisor's restart "
        "decision on that generation, or an operator's request after a stop. Generation 2 "
        "starts with no decision on generation 1: invalid_transition.",
        {"team": team},
        ("team", bad),
    )


def _callers(root: pathlib.Path) -> None:
    team, bill, support, ask = asked()
    forged = {**ask, "mail_id": f"{support.branch}:c9", "ask_id": f"{support.branch}:c9"}
    bad = support.add("message_sent", {"envelope": {**forged, "from": caller("sales")}})
    reject_at(
        root,
        "caller-mail-other-branch-rejected",
        "Rule 52: a caller's mail names its own log. support's log sends mail whose caller "
        "address is the sales thread's: invalid_transition.",
        {"team": team, "billing": bill, "support": support},
        ("support", bad),
    )
    team, bill, support, ask = asked()
    reply: Obj = {
        "mail_id": f"{bill.branch}:r1",
        "kind": "reply",
        "team": HOST_TEAM,
        "from": billing(),
        "to": caller("sales"),
        "provenance": ask["provenance"],
        "causal": {"thread_id": bill.thread, "event_id": eid(1, bill.branch)},
        "ask_id": ask["ask_id"],
        "body": {"text": "Paid."},
    }
    bad = received(support, reply)
    reject_at(
        root,
        "caller-receipt-other-branch-rejected",
        "Rule 52: a receipt to a caller is in that caller's log. support records a reply "
        "addressed to the sales thread: invalid_transition.",
        {"team": team, "billing": bill, "support": support},
        ("support", bad),
    )


def _failed_turn(bill: Log, ask: Obj) -> Obj:
    take(bill, ask)
    return bill.add("turn_completed", {"reason": "error", "code": "content_unsupported"})


def _turn_failures(root: pathlib.Path) -> None:
    team, bill, support, ask = asked()
    end = _failed_turn(bill, ask)
    other = {**ask, "ask_id": f"{support.branch}:c7", "mail_id": f"{support.branch}:c7"}
    turn_failed(bill, [other], ERROR, end)
    reject_at(
        root,
        "turn-failed-bounce-unknown-ask-rejected",
        "Rule 53: a turn_failed bounce names an unanswered ask its failed turn took. billing's "
        "bounce names an ask it never received: invalid_transition at the bounce.",
        {"team": team, "billing": bill, "support": support},
        ("billing", bill.events[-2]),
    )
    team, bill, support, ask = asked()
    _failed_turn(bill, ask)
    bad = bill.add("member_idle", {"turn_failed": ERROR})
    reject_at(
        root,
        "turn-failed-idle-before-bounces-rejected",
        "Rule 53: a failed turn's append bounces every ask the turn took before "
        "member_idle{turn_failed}. billing goes idle with support's ask unanswered: "
        "invalid_transition.",
        {"team": team, "billing": bill, "support": support},
        ("billing", bad),
    )
    team, bill, support, ask = asked()
    turn_failed(bill, [ask], ERROR, _failed_turn(bill, ask))
    bad = bill.add(
        "message_sent",
        {"envelope": {**_reply(bill, ask), "mail_id": f"{bill.branch}:r2"}},
    )
    reject_at(
        root,
        "host-member-reply-after-bounce-rejected",
        "Rules 35 and 53: an ask a failed turn bounced is answered. A later reply to it is "
        "invalid_transition.",
        {"team": team, "billing": bill, "support": support},
        ("billing", bad),
    )
    team, bill, support, ask = asked()
    take(support, _reply(bill, ask))
    failed: Obj = {"status": "failed", "error": ERROR}
    bad = support.add("ask_closed", {"ask_id": ask["ask_id"], "outcome": failed})
    reject_at(
        root,
        "ask-closed-failed-without-bounce-rejected",
        "Rule 54: an ask closes failed only on a turn_failed bounce this log received, with its "
        "error. support received billing's reply and closes the ask failed: invalid_transition.",
        {"team": team, "billing": bill, "support": support},
        ("support", bad),
    )


def _reply(bill: Log, ask: Obj) -> Obj:
    return {
        "mail_id": f"{bill.branch}:r1",
        "kind": "reply",
        "team": HOST_TEAM,
        "from": billing(),
        "to": ask["from"],
        "provenance": ask["provenance"],
        "causal": {"thread_id": bill.thread, "event_id": eid(1, bill.branch)},
        "ask_id": ask["ask_id"],
        "body": {"text": "Paid."},
    }


def _classes(root: pathlib.Path) -> None:
    team, bill, support, ask = asked()
    take(bill, ask)
    ledger: Obj = {"tenant": "acme", "team": HOST_TEAM, "name": "ledger", "generation": 1}
    note: Obj = {
        "mail_id": "0192b000-0000-7000-8000-0000000000e1:m1",
        "kind": "message",
        "team": HOST_TEAM,
        "from": ledger,
        "to": {"name": "billing", "generation": 1},
        "provenance": {**obj(ask["provenance"]), "via": [ledger]},
        "causal": {"thread_id": support.thread, "event_id": eid(2, support.branch)},
        "body": {"text": "Also check INV-1002."},
    }
    bad = received(bill, note)
    reject_at(
        root,
        "host-turn-mixed-senders-rejected",
        "Rule 55: a host member's turn takes mail decided by one rule, so of one sender class "
        "(a caller agent, a member, or the operator). billing's turn, opened by support's ask, "
        "takes a message from the host member ledger of the same request: invalid_transition.",
        {"team": team, "billing": bill, "support": support},
        ("billing", bad),
    )
