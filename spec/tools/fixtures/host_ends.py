# pyright: strict
"""The Teams Phase 2 rejections of lane 29D that need a world of their own (spec/schema/README.md,
rules 50, 52 and 53): a turn_failed code that is not its turn end's, a host team log at ids its
tenant does not derive, and a caller's mail for another tenant's host team."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .host_cases import QUESTION, asked
from .host_ids import derived
from .host_pieces import (
    TENANT,
    ask_billing,
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

MODEL_ERROR: Obj = {"code": "model_error", "message": "billing's turn failed"}
GLOBEX: Obj = {"issuer": "api", "tenant": "globex", "subject": "mallory"}


def build(root: pathlib.Path) -> None:
    _code(root)
    _ids(root)
    _tenant(root)


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
