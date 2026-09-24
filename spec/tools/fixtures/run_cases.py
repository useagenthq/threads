# pyright: strict
"""Staged cases for run completion (spec/schema/README.md, "Run completion"; Gate 1 decision
27): a run spans its input turn and every wake turn of the same request, waits for its own
members and background children, and returns the last lead turn's answer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import eid, text
from .pieces import answer, reduce_case, user
from .run_end import run_projection
from .team_pieces import (
    LEAD,
    LEAD_BRANCH,
    LOG_BRANCH,
    LOG_THREAD,
    MEMBER_BRANCH,
    MEMBER_THREAD,
    RESEARCHER,
    Route,
    at,
    body,
    envelope,
    lead_log,
    provenance,
)
from .team_steps import FAM, idle, received, start
from .wakes import KIDS, late, one_append_log, woken

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj
    from .log import Log


def build(root: pathlib.Path) -> None:
    for name, desc, log in _cases():
        reduce_case(root, (name, FAM, desc), log, {"run": run_projection(log.events)})


def _started() -> tuple[Log, Obj]:
    """Alice's run starts researcher-1 and answers; returns the log and the settlement mail."""
    lead = lead_log()
    root = text(user(lead, "Research batteries.")["event_id"])
    started_id, _ = start(lead, root, RESEARCHER, "c1")
    idle(lead, LEAD, "Started the researcher.")
    done: Obj = {"member": RESEARCHER, "status": "completed", "output": body("Prices fell.")}
    settled = envelope(
        f"{MEMBER_BRANCH}:{eid(9, MEMBER_BRANCH)}",
        "member_settled",
        Route(RESEARCHER, "lead", provenance(root)),
        at(eid(8, MEMBER_BRANCH), MEMBER_THREAD),
        monitor_id=f"{LEAD_BRANCH}:{started_id}:task",
        result=done,
    )
    return lead, settled


def _cases() -> list[tuple[str, str, Log]]:
    out: list[tuple[str, str, Log]] = []
    lead, _ = _started()
    out.append(
        (
            "run-waits-for-its-member",
            "The lead answered, but researcher-1, which this run started, has not reported: the "
            "run is still running (run() has not returned).",
            lead,
        )
    )
    lead, settled = _started()
    received(lead, settled)
    idle(lead, LEAD, "The researcher says prices fell.")
    out.append(
        (
            "run-final-answer-after-wake",
            "The member's settlement wakes the lead, whose wake turn answers again: the run is "
            "completed with the last lead turn's answer; the first stays in the timeline.",
            lead,
        )
    )
    lead, settled = _started()
    received(lead, settled)
    lead.model_request()
    lead.add("turn_completed", {"reason": "error"})
    out.append(
        (
            "run-wake-turn-fails-after-answer",
            "The wake turn of the same request fails after the first turn answered: the run is "
            "failed, not completed.",
            lead,
        )
    )
    lead = lead_log()
    user(lead, "Summarize the week.")
    answer(lead, "A quiet week.")
    request = eid(2, LOG_BRANCH)
    note = envelope(
        f"{MEMBER_BRANCH}:c1",
        "message",
        Route(RESEARCHER, "lead", provenance(request, thread=LOG_THREAD)),
        at(request, LOG_THREAD),
        body=body("A member the operator started has news."),
    )
    received(lead, note)
    lead.model_request()
    out.append(
        (
            "run-operator-member-does-not-extend",
            "After the run answers, mail of an operator request (a member the operator started) "
            "opens another lead turn. That turn belongs to the operator's request, so the run is "
            "completed with its own answer while the other turn runs.",
            lead,
        )
    )
    log = one_append_log()
    answer(log, "The dependency scan is clean.")
    out.append(
        (
            "legacy-run-waits-for-its-children",
            "A run with two background children: the first reported and woke the lead, which "
            "answered; the second still runs, so the run is still running.",
            log.copy(),
        )
    )
    woken(log, [late(log, "call_2", KIDS[1])])
    answer(log, "Both scans are clean.")
    out.append(
        (
            "legacy-run-final-answer-after-wake",
            "Both background children reported and each woke the lead: the run is completed with "
            "the last wake turn's answer.",
            log,
        )
    )
    return out
