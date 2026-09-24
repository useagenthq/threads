# pyright: strict
"""Staged Phase 1 replay cases (spec/schema/README.md, "Teams"): a member's settlement waking the
idle lead, a receipt that differs from its sender's mail, and the starting window's tree walk.
The legacy pending_wakes case is legacy_wake_rows."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, eid, sha, text
from .jcs import canonical
from .log import Log
from .pieces import user
from .team_nested import INNER, INNER_LOG
from .team_pieces import (
    LEAD,
    LEAD_BRANCH,
    MEMBER_BRANCH,
    MEMBER_THREAD,
    RESEARCHER,
    TENANT,
    Route,
    at,
    body,
    envelope,
    failed,
    lead_log,
    provenance,
    team_log,
)
from .team_steps import host, idle, materialize, received, start, tool, write_team

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
    _other_run(root)
    _nested_other_run(root)


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


def _other_run(root: pathlib.Path) -> None:
    """Alice's second run messages the member while its task turn, of her first run, is open."""
    lead = lead_log()
    first = text(user(lead, "Research batteries.")["event_id"])
    started_id, task = start(lead, first, RESEARCHER, "c1")
    idle(lead, LEAD, "Started the researcher.")
    second = text(user(lead, "Also check prices.")["event_id"])
    c = tool(lead, "send", {"to": "researcher-1", "text": "Check prices."}, "c2", "researcher-1")
    note = envelope(
        f"{LEAD_BRANCH}:c2",
        "message",
        Route(LEAD, "researcher-1", provenance(second)),
        at(text(c["event_id"])),
        body=body("Check prices."),
    )
    lead.add("message_sent", {"envelope": note})
    member = materialize(started_id, task)
    mixed = received(member, note)
    write_team(
        root,
        "team-task-turn-other-run-rejected",
        "Rule 43: the member's task turn belongs to Alice's first run, and a message of her "
        "second run is received inside it. One log shows only the task turn's principal, which "
        "is the same; the task envelope in the lead's log holds the first run's root request: "
        "invalid_transition at the receipt, in the researcher's log.",
        {"lead": lead, "researcher": member, "team": team_log()},
        {"code": "invalid_transition", "seq": mixed["seq"], "log": "researcher"},
    )


def _nested_other_run(root: pathlib.Path) -> None:
    """researcher-1 leads its own team; an operator's message to it through that team's log, a
    run of its own, is received inside the task turn its outer lead's run gave it."""
    lead = lead_log()
    first = text(user(lead, "Research batteries.")["event_id"])
    started_id, task = start(lead, first, RESEARCHER, "c1")
    inner: Obj = {"id": INNER, "log_branch_id": INNER_LOG[0]}
    member = materialize(started_id, task, team=inner)
    own = Log(INNER_LOG[0], thread=INNER_LOG[1])
    nested_lead: Obj = {"tenant": TENANT, "team": INNER, "name": "researcher", "generation": 1}
    own.add("team_opened", {"team": INNER, "lead": nested_lead, "lead_thread_id": MEMBER_THREAD})
    request = "0192d000-0000-7000-8000-0000000000e1"
    here = eid(own.seq + 1, INNER_LOG[0])
    prov: Obj = {
        "principal": ALICE,
        "root_request": {"thread_id": INNER_LOG[1], "event_id": here},
        "via": [],
    }
    note_body = body("Check prices too.")
    host(
        own,
        "operator_request",
        {
            "request_id": request,
            "op": "send",
            "principal": ALICE,
            "body_hash": sha(canonical({"to": "researcher", "text": "Check prices too."})),
            "provenance": prov,
        },
    )
    own.add(
        "message_policy_decided",
        {
            "op": "send",
            "decision": "allow",
            "source": "team",
            "target": "researcher",
            "request_id": request,
        },
    )
    note: Obj = {
        "mail_id": f"{INNER_LOG[0]}:{request}",
        "kind": "message",
        "team": INNER,
        "from": {"operator": request},
        "to": {"name": "researcher", "generation": 1},
        "provenance": prov,
        "causal": at(here, INNER_LOG[1]),
        "body": note_body,
    }
    own.add("message_sent", {"envelope": note})
    mixed = received(member, note)
    write_team(
        root,
        "team-nested-task-turn-other-run-rejected",
        "Rule 43, for a nested lead: researcher-1's task turn belongs to the outer lead's run, "
        "and an operator's message through researcher-1's own team, a run of its own, is "
        "received inside it. The mail is the inner team's, the task envelope the outer team's; "
        "the task-turn clause is checked whatever team the mail belongs to: invalid_transition "
        "at the receipt, in the researcher's log.",
        {"inner": own, "lead": lead, "researcher": member, "team": team_log()},
        {"code": "invalid_transition", "seq": mixed["seq"], "log": "researcher"},
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
