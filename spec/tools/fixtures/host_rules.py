# pyright: strict
"""The Teams Phase 2 rejections of lane 29D (spec/schema/README.md, semantic rules 50, 52-55):
each log breaks one rule at one event, which the reference validator (ref_host.py) catches there.
Supervision's rejections (rule 51) are staged in host_super.py until lane 29E's build."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import eid, num, obj
from .host_cases import asked, write_host
from .host_pieces import (
    HOST_TEAM,
    billing,
    caller,
    ended_bounce,
    host_start,
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
ENDED: Obj = {
    "member": billing(),
    "status": "failed",
    "error": {"code": "pin_unavailable", "message": "rebind failed: pin_unavailable"},
}


def reject_at(
    root: pathlib.Path, name: str, desc: str, logs: dict[str, Log], at: tuple[str, Obj]
) -> None:
    label, e = at
    error: Obj = {"code": "invalid_transition", "seq": num(e["seq"]), "log": label}
    write_host(root, name, desc, logs, error)


def build(root: pathlib.Path) -> None:
    _placement(root)
    _callers(root)
    _end_bounces(root)
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


def _end_bounces(root: pathlib.Path) -> None:
    team, bill, support, ask = asked()
    take(bill, ask)
    ended = bill.add("member_ended", {"result": ENDED})
    cancelled: Obj = {**ENDED, "status": "cancelled"}
    wrong: Obj = {**ended_bounce(bill, ask, ENDED, ended), "result": cancelled}
    bad = bill.add("message_sent", {"envelope": wrong})
    reject_at(
        root,
        "end-bounce-result-mismatch-rejected",
        "Rule 43: a bounce whose causal is its sender's own member_ended carries that end's "
        "result. billing's end bounces support's taken ask with a cancelled result instead of "
        "the end's: invalid_transition at the bounce.",
        {"team": team, "billing": bill, "support": support},
        ("billing", bad),
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
