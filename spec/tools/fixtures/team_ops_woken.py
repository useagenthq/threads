# pyright: strict
"""Team op vectors for a lead in a woken turn (spec/schema/README.md, "Background wakes"): a
background child's late result woke the idle lead, and the wake turn belongs to the run that
spawned the child, so mail it sends and members it starts carry that run's provenance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .team_ops_start import NEW_THREAD
from .team_ops_worlds import Vec, go_idle, pending_call, run, team
from .team_pieces import LEAD_BRANCH, ref
from .wakes import KIDS, late, spawn_background, woken

if TYPE_CHECKING:
    from .jcs import Obj
    from .ops_world import World

P, S, R = "message_policy_decided", "message_sent", "tool_result"


def _woken_lead() -> World:
    """The lead's run spawned a background scanner, went idle, and the scanner's late result
    woke it."""
    w = team()
    run(w)
    lead = w.logs["lead"]
    spawn_background(lead, "c5", KIDS[0])
    go_idle(w, "lead", "The scan is running.")
    woken(lead, [late(lead, "c5", KIDS[0])])
    return w


def woken_vectors() -> list[Vec]:
    w = _woken_lead()
    send = pending_call(w, "lead", "send", {"to": "researcher-1", "text": "Scan done."}, "c6")
    out = [
        Vec(
            "send-from-woken-turn",
            "4.5",
            "The lead's wake turn sends researcher-1 a message: the turn belongs to the run that "
            "spawned the woken child, so the message carries that run's principal and root "
            "request (the lead's user_input), exactly as a send in the run's first turn would.",
            w,
            "send",
            "lead",
            send,
            {"id": f"{LEAD_BRANCH}:c6", "status": "sent"},
            {"lead": [P, S, R]},
        )
    ]
    w = _woken_lead()
    start: Obj = {
        **pending_call(w, "lead", "start", {"agent": "researcher", "task": "Recheck."}, "c6"),
        "thread_id": NEW_THREAD,
    }
    out.append(
        Vec(
            "start-from-woken-turn",
            "4.10",
            "The lead's wake turn starts researcher-2: member_started and its task carry the "
            "spawning run's provenance, so the new member belongs to that run.",
            w,
            "start",
            "lead",
            start,
            {"member": ref("researcher-2"), "status": "started"},
            {"lead": [P, "member_started", S, R]},
        )
    )
    return out
