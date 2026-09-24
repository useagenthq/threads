# pyright: strict
"""Staged Phase 1 replay cases (spec/schema/README.md, "Teams"): a member's settlement waking the
idle lead, a receipt that differs from its sender's mail, and the starting window's tree walk.
The legacy pending_wakes case is legacy_wake_rows."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import eid, text
from .pieces import user
from .team_pieces import (
    LEAD,
    LEAD_BRANCH,
    MEMBER_BRANCH,
    MEMBER_THREAD,
    RESEARCHER,
    Route,
    at,
    body,
    envelope,
    failed,
    lead_log,
    provenance,
    team_log,
)
from .team_steps import idle, materialize, received, start, tool, write_team

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj

BOB: Obj = {"issuer": "api", "tenant": "acme", "subject": "bob"}
KIDS = ("0192a000-0000-7000-8000-0000000000c9", "0192a000-0000-7000-8000-0000000000ca")


def build(root: pathlib.Path) -> None:
    _tree(root, notified=False)
    _tree(root, notified=True)
    _settle(root)
    _receipt_mismatch(root)


def _settle(root: pathlib.Path) -> None:
    lead = lead_log()
    root_event = text(user(lead, "Research batteries.")["event_id"])
    started_id, task = start(lead, root_event, RESEARCHER, "c1")
    idle(lead, LEAD, "Started the researcher.")

    member = materialize(started_id, task)
    done = idle(member, RESEARCHER, "Battery prices fell.")
    settled = envelope(
        f"{MEMBER_BRANCH}:{eid(member.seq + 1, MEMBER_BRANCH)}",
        "member_settled",
        Route(RESEARCHER, "lead", provenance(root_event)),
        at(text(member.events[-1]["event_id"]), MEMBER_THREAD),
        monitor_id=f"{LEAD_BRANCH}:{started_id}:task",
        result=done,
    )
    member.add("message_sent", {"envelope": settled})

    received(lead, settled)  # the task notification alone wakes the idle lead
    idle(lead, LEAD, "The researcher says battery prices fell.")
    write_team(
        root,
        "team-settle-wakes-lead",
        "The lead starts researcher-1 and goes idle. The member's task is its user_input; it "
        "answers, goes idle and fires the lead's task monitor with member_settled. That "
        "notification alone opens the idle lead's next turn, of the same request. The receipt "
        "copies the sender's envelope byte for byte, and the logs rebuild every index row: the "
        "task and the notification consumed, the task monitor gone, both members idle.",
        {"lead": lead, "researcher": member, "team": team_log()},
    )


def _receipt_mismatch(root: pathlib.Path) -> None:
    lead = lead_log()
    root_event = text(user(lead, "Research batteries.")["event_id"])
    started_id, task = start(lead, root_event, RESEARCHER, "c1")
    c = tool(lead, "send", {"to": "researcher-1", "text": "Keep it short."}, "c2", "researcher-1")
    note = envelope(
        f"{LEAD_BRANCH}:c2",
        "message",
        Route(LEAD, "researcher-1", provenance(root_event)),
        at(text(c["event_id"])),
        body=body("Keep it short."),
    )
    lead.add("message_sent", {"envelope": note})
    member = materialize(started_id, task)
    forged = received(member, {**note, "body": body("Take as long as you like.")})
    write_team(
        root,
        "team-receipt-envelope-mismatch-rejected",
        "Rule 43: the member's receipt of the lead's message carries another body than the "
        "lead's message_sent. Only the two logs together show it: invalid_transition at the "
        "receipt, in the researcher's log.",
        {"lead": lead, "researcher": member, "team": team_log()},
        {"code": "invalid_transition", "seq": forged["seq"], "log": "researcher"},
    )


def _tree(root: pathlib.Path, *, notified: bool) -> None:
    """The lead started researcher-1, whose branch doesn't exist: the starting window, or, once
    the lead received its task notification, a corrupt tree."""
    lead = lead_log()
    root_event = text(user(lead, "Research batteries.")["event_id"])
    started_id, _ = start(lead, root_event, RESEARCHER, "c1")
    idle(lead, LEAD, "Started the researcher.")
    logs = {"lead": lead, "team": team_log()}
    if not notified:
        write_team(
            root,
            "team-tree-starting-member-pending",
            "researcher-1 is in the starting window (a row, no branch, no notification in the "
            "lead's log): cost and usage walks count it zero as pending, and complete stays true "
            "since it has made no model request.",
            logs,
        )
        return
    ended = failed(RESEARCHER, "pin_unavailable")
    notice = envelope(
        f"{MEMBER_BRANCH}:{eid(9, MEMBER_BRANCH)}",
        "member_ended",
        Route(RESEARCHER, "lead", provenance(root_event)),
        at(eid(8, MEMBER_BRANCH), MEMBER_THREAD),
        monitor_id=f"{LEAD_BRANCH}:{started_id}:task",
        result=ended,
    )
    received(lead, notice)
    write_team(
        root,
        "team-tree-missing-branch-after-notice-rejected",
        "The lead received researcher-1's task notification, so its branch must exist, but no "
        "log has it: the tree walk is log_corrupt.",
        logs,
        {"code": "log_corrupt", "log": "lead"},
    )
