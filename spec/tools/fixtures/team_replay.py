# pyright: strict
"""Staged replay cases for background wakes and teams (spec/schema/README.md, "Teams"): a legacy
late result with its woken in one append, a member's settlement waking the idle lead, and a
receipt that differs from its sender's mail."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import eid, text, tokens
from .log import Log
from .pieces import answer, call, reduce_case, result, started, user
from .projections import children
from .team_index import pending_wakes
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
    lead_log,
    provenance,
    team_log,
)
from .team_steps import FAM, host, idle, materialize, received, start, tool, write_team
from .teams import catalog_specs

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj

KIDS = ("0192a000-0000-7000-8000-0000000000c9", "0192a000-0000-7000-8000-0000000000ca")


def build(root: pathlib.Path) -> None:
    _legacy_wake(root)
    _settle(root)
    _receipt_mismatch(root)


def _spawn(log: Log, cid: str, kid: str) -> None:
    call(log, "spawn_agent", {"agent": "scanner", "prompt": "Do your part."}, cid)
    spawned: Obj = {
        "call_id": cid,
        "child_thread_id": kid,
        "agent_name": "scanner",
        "mode": "background",
        "isolation": "none",
    }
    log.add("agent_spawned", spawned)
    result(log, cid, "scanner started in the background", origin="deferred")


def _legacy_wake(root: pathlib.Path) -> None:
    log = Log()
    started(log, catalog_specs(("spawn_agent",)))
    user(log, "Scan the dependencies and the licenses in the background.")
    _spawn(log, "call_1", KIDS[0])
    _spawn(log, "call_2", KIDS[1])
    answer(log, "Both scans are running.")
    finished: Obj = {"child_thread_id": KIDS[0], "status": "completed", "usage": tokens(40, 8)}
    log.add("agent_finished", finished)
    late: Obj = {
        "call_id": "call_1",
        "is_error": False,
        "completeness": "complete",
        "preview": "No vulnerable dependencies.",
    }
    cause = log.add("tool_result_late", late)
    host(log, "woken", {"causes": [cause["event_id"]]})
    reduce_case(
        root,
        (
            "legacy-wake-in-the-late-result-append",
            FAM,
            "Two background children run; the first ends while the lead has no open turn and no "
            "cancel barrier. One append records its agent_finished, its tool_result_late and a "
            "woken naming it, and woken opens the lead's next turn (in_turn). The second child "
            "still runs, so its pending_wakes row stays. subagent-background-notify is a log "
            "written before woken existed: it stays idle, never woken retroactively.",
        ),
        log,
        {"children": children(log), "pending_wakes": pending_wakes([log])},
    )


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
