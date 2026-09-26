# pyright: strict
"""The staged Teams Phase 2 supervision cases (spec/schema/README.md, semantic rules 50-51): a
restart, the cap, asks across a restart, the two ends that happen with a turn open, and the
supervisor rejections. Staged until lane 29E's build reads them; lane 29D's cases are in
host_cases.py, host_rules.py and host_ends.py, which its build moved into the corpus."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import num, obj
from .host_cases import asked, close_ask, write_host
from .host_pieces import (
    POLICY,
    billing,
    billing_log,
    ended_bounce,
    host_start,
    host_team_log,
    take,
)
from .host_rules import reject_at
from .team_pieces import team_log

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj
    from .log import Log

OWN: Obj = {
    "scope": "thread",
    "limit": "max_cost_nanos",
    "limit_value": 5_000_000_000,
    "observed": 5_200_000_000,
    "observed_is_upper_bound": False,
}


def build(root: pathlib.Path) -> None:
    _supervised(root)
    _across_restart(root)
    _own_budget(root)
    _rebind(root)
    _placement(root)
    _counts(root)
    _other_member(root)


def failure(gen: int, code: str = "pin_mismatch") -> Obj:
    return {
        "member": billing(gen),
        "status": "failed",
        "error": {"code": code, "message": f"rebind failed: {code}"},
    }


def end_gen(bill: Log, gen: int = 1, code: str = "pin_unavailable") -> Obj:
    return bill.add("member_ended", {"result": failure(gen, code)})


def decide(team: Log, ended: Obj, action: str, count: int, *, policy: Obj = POLICY) -> None:
    """The supervisor's decision on the generation `ended` (its member_ended) ends."""
    gen = num(obj(obj(obj(ended["data"])["result"])["member"])["generation"])
    data: Obj = {
        "member": billing(gen),
        "ended": {"branch_id": ended["branch_id"], "seq": ended["seq"]},
        "action": action,
        "restarts_in_window": count,
        "policy": policy,
    }
    team.add("supervisor_decided", data)
    if action == "restart":
        team.add("member_started", host_start(gen + 1, restart_of=gen))


def _supervised(root: pathlib.Path) -> None:
    team, bill1 = host_team_log(), billing_log(1)
    decide(team, end_gen(bill1), "restart", 0)
    write_host(
        root,
        "host-supervisor-restart",
        "Teams Phase 2 (E): billing's generation 1 fails its rebind (member_ended{failed "
        "pin_unavailable}). The host team log's writer decides once: supervisor_decided{restart, "
        "restarts_in_window 0} and, in the same append, member_started of generation 2 with "
        "restart_of 1. Generation 2 opens a new, empty thread and is idle.",
        {"team": team, "billing": bill1, "billing2": billing_log(2)},
    )
    team, bill1, bill2 = host_team_log(), billing_log(1), billing_log(2)
    capped: Obj = {**POLICY, "max_restarts": 1}
    decide(team, end_gen(bill1), "restart", 0, policy=capped)
    ended2 = bill2.add("member_ended", {"result": failure(2)})
    decide(team, ended2, "stop", 1, policy=capped)
    write_host(
        root,
        "host-supervisor-stop-at-cap",
        "Teams Phase 2 (E): with maxRestarts 1, generation 1 fails and is restarted; generation "
        "2 fails within the window, so restarts_in_window is 1 and the decision is stop. The "
        "name stays ended: no generation 3.",
        {"team": team, "billing": bill1, "billing2": bill2},
    )


def _across_restart(root: pathlib.Path) -> None:
    team, bill1, support, ask = asked()
    bill1.add("mail_refused", {"mail_id": ask["mail_id"], "code": "member_ended"})
    result_ = failure(1)
    ended = bill1.add("member_ended", {"result": result_})
    bounce: Obj = {
        **ended_bounce(bill1, ask, result_, bill1.events[-2]),
    }
    bill1.add("message_sent", {"envelope": bounce})
    decide(team, ended, "restart", 0)
    ended_as: Obj = {"status": "member_ended", "result": result_}
    close_ask(support, bounce, ended_as, {"status": "member_ended", "result": result_})
    write_host(
        root,
        "host-ask-across-restart-never-stale",
        "Teams Phase 2 (E, the stale negative): support's ask is pending when billing's "
        "generation 1 ends. Its end append refuses the ask (mail_refused{member_ended}, the row "
        "returned) and bounces it to the caller with the result; the supervisor restarts "
        "generation 2. The ask closes member_ended; no mail is ever stale, since a sender binds "
        "the current generation and an end refuses every pending row.",
        {"team": team, "billing": bill1, "billing2": billing_log(2), "support": support},
    )


def _own_budget(root: pathlib.Path) -> None:
    team, bill, support, ask = asked()
    take(bill, ask)
    bill.add("budget_exceeded", OWN)
    bill.add("turn_completed", {"reason": "budget_exhausted"})
    result: Obj = {"member": billing(), "status": "budget_exhausted", "budget": OWN}
    ended = bill.add("member_ended", {"result": result})
    bill.add("message_sent", {"envelope": ended_bounce(bill, ask, result, ended)})
    decide(team, ended, "stop", 0)
    write_host(
        root,
        "host-member-own-budget-ends",
        "Teams Phase 2 (rule 53's scope): billing's own thread budget, a lifetime cap for a host "
        "member, runs out in the turn that took support's ask. The append ends the member "
        "(budget_exceeded{scope: thread}, turn_completed{budget_exhausted}, "
        "member_ended{budget_exhausted}), so rule 53 does not apply: no turn_failed bounce and "
        "no member_idle. The end bounces the ask it had taken instead, member_ended with the "
        "end's result, rather than leaving support parked until the deadline. The supervisor "
        "stops the member: an own-budget end never restarts.",
        {"team": team, "billing": bill, "support": support},
    )


def _rebind(root: pathlib.Path) -> None:
    team, bill, support, ask = asked()
    take(bill, ask)
    bill.add("turn_completed", {"reason": "error", "code": "pin_unavailable"})
    result = failure(1, "pin_unavailable")
    ended = bill.add("member_ended", {"result": result})
    bill.add("message_sent", {"envelope": ended_bounce(bill, ask, result, ended)})
    decide(team, ended, "restart", 0)
    write_host(
        root,
        "host-member-rebind-fails-mid-turn",
        "Teams Phase 2 (rule 53's scope): billing's rebind fails with a turn open, the turn that "
        "took support's ask. The append is the failed rebind's: turn_completed{error, "
        "pin_unavailable} then member_ended{failed}, so rule 53 does not apply; the end bounces "
        "the taken ask member_ended. The supervisor restarts generation 2 in a new thread.",
        {"team": team, "billing": bill, "billing2": billing_log(2), "support": support},
    )


def _decision(ended: Obj, action: str, count: int) -> Obj:
    return {
        "member": billing(1),
        "ended": {"branch_id": ended["branch_id"], "seq": ended["seq"]},
        "action": action,
        "restarts_in_window": count,
        "policy": POLICY,
    }


def _placement(root: pathlib.Path) -> None:
    lead_team, bill = team_log(), billing_log()
    bad = lead_team.add("supervisor_decided", _decision(end_gen(bill), "restart", 0))
    reject_at(
        root,
        "supervisor-decided-in-lead-team-rejected",
        "Rule 50: supervisor_decided belongs to a host team's log only; a lead's team log takes "
        "none: invalid_transition there, in the team log.",
        {"team": lead_team, "billing": bill},
        ("team", bad),
    )


def _counts(root: pathlib.Path) -> None:
    team, bill = host_team_log(), billing_log()
    ended = end_gen(bill)
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
    bad = team.add("supervisor_decided", _decision(end_gen(bill), "restart", 1))
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


def _other_member(root: pathlib.Path) -> None:
    team, bill1, bill2 = host_team_log(), billing_log(1), billing_log(2)
    first = bill1.add("member_ended", {"result": failure(1)})
    decide(team, first, "restart", 0)
    bill2.add("member_ended", {"result": failure(2)})
    decision = team.add(
        "supervisor_decided",
        {
            "member": billing(2),
            "ended": {"branch_id": first["branch_id"], "seq": first["seq"]},
            "action": "restart",
            "restarts_in_window": 1,
            "policy": POLICY,
        },
    )
    reject_at(
        root,
        "supervisor-ended-other-member-rejected",
        "Rule 51 across the logs: a decision's ended is its own member's member_ended. The "
        "decision on generation 2 names generation 1's end: invalid_transition at the decision.",
        {"team": team, "billing": bill1, "billing2": bill2},
        ("team", decision),
    )
