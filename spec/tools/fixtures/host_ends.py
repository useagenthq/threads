# pyright: strict
"""The staged Teams Phase 2 cases of a host member that ends with a turn open (its own lifetime
budget, a failed rebind), and the tenant, turn-code and supervisor-member rejections
(spec/schema/README.md, rules 50-53)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .host_cases import QUESTION, asked, decide, failure, write_host
from .host_ids import derived
from .host_pieces import (
    POLICY,
    TENANT,
    ask_billing,
    billing,
    billing_log,
    caller_log,
    host_team_log,
    take,
    turn_failed,
)
from .host_rules import reject_at
from .log import Log
from .pieces import user

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj

OWN: Obj = {
    "scope": "thread",
    "limit": "max_cost_nanos",
    "limit_value": 5_000_000_000,
    "observed": 5_200_000_000,
    "observed_is_upper_bound": False,
}
MODEL_ERROR: Obj = {"code": "model_error", "message": "billing's turn failed"}
GLOBEX: Obj = {"issuer": "api", "tenant": "globex", "subject": "mallory"}


def build(root: pathlib.Path) -> None:
    _own_budget(root)
    _rebind(root)
    _code(root)
    _ids(root)
    _tenant(root)
    _other_member(root)


def _own_budget(root: pathlib.Path) -> None:
    team, bill, support, ask = asked()
    take(bill, ask)
    bill.add("budget_exceeded", OWN)
    bill.add("turn_completed", {"reason": "budget_exhausted"})
    result: Obj = {"member": billing(), "status": "budget_exhausted", "budget": OWN}
    ended = bill.add("member_ended", {"result": result})
    decide(team, ended, "stop", 0)
    write_host(
        root,
        "host-member-own-budget-ends",
        "Teams Phase 2 (rule 53's scope): billing's own thread budget, a lifetime cap for a host "
        "member, runs out in the turn that took support's ask. The append ends the member "
        "(budget_exceeded{scope: thread}, turn_completed{budget_exhausted}, "
        "member_ended{budget_exhausted}), so rule 53 does not apply: no turn_failed bounce and no "
        "member_idle. The taken ask stays open until its deadline, and support stays parked. The "
        "supervisor stops the member: an own-budget end never restarts.",
        {"team": team, "billing": bill, "support": support},
    )


def _rebind(root: pathlib.Path) -> None:
    team, bill, support, ask = asked()
    take(bill, ask)
    bill.add("turn_completed", {"reason": "error", "code": "pin_unavailable"})
    ended = bill.add("member_ended", {"result": failure(1)})
    decide(team, ended, "restart", 0)
    write_host(
        root,
        "host-member-rebind-fails-mid-turn",
        "Teams Phase 2 (rule 53's scope): billing's rebind fails with a turn open, the turn that "
        "took support's ask. The append is the failed rebind's: turn_completed{error, "
        "pin_unavailable} then member_ended{failed}, so rule 53 does not apply and the taken "
        "ask waits for its deadline. The supervisor restarts generation 2 in a new thread.",
        {"team": team, "billing": bill, "billing2": billing_log(2), "support": support},
    )


def _code(root: pathlib.Path) -> None:
    team, bill, support, ask = asked()
    take(bill, ask)
    end = bill.add("turn_completed", {"reason": "max_turns"})
    turn_failed(bill, [ask], MODEL_ERROR, end)
    reject_at(
        root,
        "turn-failed-code-mismatch-rejected",
        "Rule 53: a failed turn's TurnFailure code is its turn end's, and max_turns maps to "
        "max_turns. billing's bounce records model_error for a max_turns end: "
        "invalid_transition at the bounce.",
        {"team": team, "billing": bill, "support": support},
        ("billing", bill.events[-2]),
    )


def _ids(root: pathlib.Path) -> None:
    team = Log(derived("log_branch", TENANT), thread=derived("log_thread", TENANT))
    opened = team.add(
        "team_opened", {"team": derived("team", "globex"), "kind": "host", "tenant": TENANT}
    )
    reject_at(
        root,
        "host-team-ids-not-derived-rejected",
        "Rule 50: a host team's log is at the ids its tenant derives. acme's host team log "
        "names globex's team id: invalid_transition at team_opened.",
        {"team": team},
        ("team", opened),
    )


def _tenant(root: pathlib.Path) -> None:
    team, bill = host_team_log(), billing_log()
    support = caller_log()
    root_event = user(support, QUESTION)
    ask_billing(support, "support", str(root_event["event_id"]), "c1", QUESTION, principal=GLOBEX)
    sent = support.events[-2]
    reject_at(
        root,
        "host-caller-other-tenant-rejected",
        "Rule 52: host team mail belongs to the team its provenance principal's tenant derives. "
        "support's ask of acme's billing carries a principal of tenant globex: "
        "invalid_transition at the ask.",
        {"team": team, "billing": bill, "support": support},
        ("support", sent),
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
