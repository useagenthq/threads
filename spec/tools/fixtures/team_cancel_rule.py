# pyright: strict
"""The staged post-cancel cases (spec/schema/README.md, rule 37): after a tree cancel reaches a
team member, its log opens no new turn; a cancelled lead still takes a later run's input. Staged
until the runtimes check the member half (lane 21E)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, obj
from .pieces import user
from .team_ops_worlds import dispatch, run, take, team
from .team_pieces import lead_log, team_log
from .team_steps import received, write_team

if TYPE_CHECKING:
    import pathlib


def build(root: pathlib.Path) -> None:
    _member(root)
    _lead(root)


def _member(root: pathlib.Path) -> None:
    w = team()
    run(w)
    dispatch(w, "lead", "cancel", {"member": "researcher-1"}, "c3")
    dispatch(w, "lead", "send", {"to": "researcher-1", "text": "One more thing."}, "c4")
    take(w, "researcher")  # the cancel's receipt and its barrier; the message stays pending
    member = w.logs["researcher"]
    barrier = next(e for e in reversed(member.events) if e["type"] == "cancel_requested")
    member.add("cancelled", {"request_event_id": barrier["event_id"]})
    member.add("turn_completed", {"reason": "cancelled"})
    late = received(member, obj(w.pending("researcher-1")[0]["envelope"]))
    write_team(
        root,
        "member-cancelled-then-turn-rejected",
        "Rule 37: researcher-1 applied the lead's tree cancel (message_received, "
        "cancel_requested{scope: tree}) and its turn ended cancelled, but its log then takes the "
        "lead's message as a new turn. After a tree cancel a team member's log opens no turn "
        "(only a resumed may continue the turn already open), even before its member_ended: "
        "invalid_transition at the receipt, in the researcher's log.",
        {"lead": w.logs["lead"], "researcher": member, "team": w.logs["team"]},
        {"code": "invalid_transition", "seq": late["seq"], "log": "researcher"},
    )


def _lead(root: pathlib.Path) -> None:
    lead = lead_log()
    user(lead, "Research batteries.")
    barrier = lead.add("cancel_requested", {"scope": "tree"}, actor="user", principal=ALICE)
    lead.add("cancelled", {"request_event_id": barrier["event_id"]})
    lead.add("turn_completed", {"reason": "cancelled"})
    user(lead, "Try again, only prices this time.")
    write_team(
        root,
        "lead-cancelled-then-new-run",
        "Rule 37 stops only a member: the user cancels the lead's run (a tree cancel, its turn "
        "ended cancelled), then starts a later run with a new user_input. A cancel ends that "
        "run's turn only, so the lead takes it and the team stays usable.",
        {"lead": lead, "team": team_log()},
    )
