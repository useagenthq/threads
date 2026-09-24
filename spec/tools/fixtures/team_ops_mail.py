# pyright: strict
"""Team op vectors on the recipient's side (design §4.7 consume, §4.10 materialize and a failed
rebind, §4.14 cancel's application): batches, control mail, parks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .ops_send import send
from .team_ops_worlds import (
    REQUESTS,
    Vec,
    dispatch,
    go_idle,
    operator,
    run,
    take,
    team,
    writer_asks,
)
from .team_pieces import (
    LEAD_BRANCH,
    LOG_BRANCH,
    MEMBER_BRANCH,
    RESEARCHER,
    WRITER_BRANCH,
)

if TYPE_CHECKING:
    from .jcs import Obj
    from .ops_world import World

RECEIVED, SENT = "message_received", "message_sent"
CLOSE = ["ask_closed", "resumed", "tool_result"]
ASK = f"{WRITER_BRANCH}:c1"


def _consume(  # noqa: PLR0913, PLR0917 - one argument per vector field
    name: str, section: str, desc: str, w: World, by: str, outcome: Obj, types: list[str]
) -> Vec:
    return Vec(name, section, desc, w, "consume", by, {}, outcome, {by: types} if types else {})


def _taken(*ids: str) -> Obj:
    return {"status": "consumed", "mail_ids": list(ids)}


def _both(*, researcher_idle: bool = False) -> World:
    w = team(writer=True)
    run(w)
    run(w, "writer-1")
    if researcher_idle:
        go_idle(w, "researcher", "Battery prices fell.")
    return w


def _lead_sends(w: World, to: str = "researcher-1", cid: str = "c3") -> None:
    dispatch(w, "lead", "send", {"to": to, "text": "Keep it short."}, cid)


def _operator_sends(w: World) -> None:
    send(w, "team", operator(REQUESTS[1], {"to": RESEARCHER, "text": "Status?"}))


def consume_vectors() -> list[Vec]:
    out: list[Vec] = []
    w = _both(researcher_idle=True)
    _lead_sends(w)
    out.append(
        _consume(
            "consume-message-opens-idle-member",
            "4.7",
            "The idle researcher's one pending message is taken: message_received copies the whole "
            "envelope, opens a turn and the row becomes running.",
            w,
            "researcher",
            _taken(f"{LEAD_BRANCH}:c3"),
            [RECEIVED],
        )
    )
    w = _both(researcher_idle=True)
    _operator_sends(w)
    _lead_sends(w)
    out.append(
        _consume(
            "consume-batch-shares-one-request",
            "4.7",
            "Two pending messages of different requests (the operator's, created first, and the "
            "lead's run): the batch is the longest prefix sharing the first row's principal and "
            "root "
            "request, so only the operator's is taken; the lead's stays pending for a later turn.",
            w,
            "researcher",
            _taken(f"{LOG_BRANCH}:{REQUESTS[1]}"),
            [RECEIVED],
        )
    )
    w = _both()
    _operator_sends(w)
    _lead_sends(w)
    out.append(
        _consume(
            "consume-mid-turn-takes-its-request",
            "4.7",
            "The researcher is mid-turn on the lead's run: only the row of that run is taken, and "
            "the operator's (created first) stays pending.",
            w,
            "researcher",
            _taken(f"{LEAD_BRANCH}:c3"),
            [RECEIVED],
        )
    )
    w = _both()
    writer_asks(w)
    _lead_sends(w, "writer-1")
    out.append(
        _consume(
            "consume-parked-leaves-ordinary-mail",
            "4.7",
            "The writer is parked on its ask: the lead's ordinary message stays pending.",
            w,
            "writer",
            _taken(),
            [],
        )
    )
    out.append(
        _consume(
            "consume-nothing-pending",
            "4.7",
            "Nothing is addressed to the researcher.",
            _both(),
            "researcher",
            {"status": "nothing_pending"},
            [],
        )
    )
    return out + _park_vectors() + _cancel_applied()


def _lead_idle_writer_parked() -> World:
    w = _both()
    go_idle(w, "lead", "Started the team.")
    writer_asks(w)
    return w


def _writer_answered(w: World) -> None:
    """The researcher replies, the writer closes its ask and goes idle: its task monitor fires."""
    take(w, "researcher")
    dispatch(w, "researcher", "reply", {"ask_id": ASK, "text": "Batteries."}, "c2")
    take(w, "writer")
    go_idle(w, "writer", "Wrote it up.")


def _park_vectors() -> list[Vec]:
    w = _lead_idle_writer_parked()
    notice = _mail_to(w, "lead")
    out = [
        _consume(
            "consume-member-parked-parks-lead",
            "4.7",
            "writer-1, which the idle lead started, parked on an ask; its one member_parked notice "
            "is control mail: the lead's task monitor row still exists, so message_received and "
            "parked{kind: member} in one append. No turn opens.",
            w,
            "lead",
            _taken(*notice),
            [RECEIVED, "parked"],
        )
    ]
    w = _lead_idle_writer_parked()
    _writer_answered(w)
    notices = _mail_to(w, "lead")
    out.append(
        _consume(
            "consume-member-parked-after-settle",
            "4.7",
            "The writer parked, then settled before the lead consumed anything. The member_parked "
            "notice finds its task monitor row gone (the settlement deleted it): receipt only. The "
            "settlement is then ordinary mail and opens the idle lead's turn.",
            w,
            "lead",
            _taken(*notices),
            [RECEIVED, RECEIVED],
        )
    )
    w = _lead_idle_writer_parked()
    take(w, "lead")
    _writer_answered(w)
    out.append(
        _consume(
            "consume-settle-resolves-member-park",
            "4.7",
            "The lead is parked on writer-1 ({kind: member}); the writer's settlement is control "
            "mail: message_received then resumed, which clears the last park and opens the turn.",
            w,
            "lead",
            _taken(*_mail_to(w, "lead")),
            [RECEIVED, "resumed"],
        )
    )
    return out


def _mail_to(w: World, name: str) -> list[str]:
    return [str(m["mail_id"]) for m in w.pending(name)]


def _cancel_applied() -> list[Vec]:
    w = _both()
    dispatch(w, "lead", "cancel", {"member": "researcher-1"}, "c3")
    out = [
        _consume(
            "cancel-applied-running-member",
            "4.14",
            "The researcher's writer takes the cancel mail at a step boundary: message_received "
            "and "
            "today's barrier, cancel_requested{scope: tree}, with actor {host, "
            "provenance.principal}. "
            "The barrier rules then end the member cancelled.",
            w,
            "researcher",
            _taken(f"{LEAD_BRANCH}:c3"),
            [RECEIVED, "cancel_requested"],
        )
    ]
    w = _both()
    writer_asks(w)
    dispatch(w, "lead", "cancel", {"member": "writer-1"}, "c3")
    out.append(
        _consume(
            "cancel-applied-parked-asker",
            "4.8, 4.14",
            "Cancel mail is control mail, taken while the writer is parked on its ask: the "
            "receipt, "
            "the barrier, and the ask closed cancelled with resumed and the call's result.",
            w,
            "writer",
            _taken(f"{LEAD_BRANCH}:c3"),
            [RECEIVED, "cancel_requested", *CLOSE],
        )
    )
    return out


def materialize_vectors() -> list[Vec]:
    def inp(rebind: str) -> Obj:
        return {
            "member": "researcher-1",
            "label": "researcher",
            "branch_id": MEMBER_BRANCH,
            "rebind": rebind,
        }

    out = [
        Vec(
            "materialize-opens-branch",
            "4.10",
            "The first consume of a starting member opens its branch: thread_started (the "
            "parent member_started names) and user_input{team_task} with the task, which "
            "consumes the task row; the row becomes running with its branch.",
            team(),
            "materialize",
            "researcher",
            inp("ok"),
            {"status": "materialized"},
            {"researcher": ["thread_started", "user_input"]},
        ),
        Vec(
            "materialize-not-starting",
            "4.10",
            "The row check in the transaction: the member is no longer starting, so nothing is "
            "committed.",
            _both(),
            "materialize",
            "researcher",
            inp("ok"),
            {"status": "not_starting"},
            {},
        ),
    ]
    w = team(writer=True)
    run(w, "writer-1")
    _lead_sends(w)
    writer_asks(w)
    out.append(
        Vec(
            "failed-rebind-pin-unavailable",
            "4.10",
            "The rebind finds the definition unregistered: one append opens the branch with "
            "thread_started, the task's user_input, turn_completed{error: pin_unavailable} and "
            "member_ended{failed}; one member_ended notification for the lead's task monitor; "
            "then, "
            "in (created_at, mail_id) order, a mail_refused and a bounce for the lead's message "
            "and "
            "for the writer's ask (whose bounce carries the ask_id and the result).",
            w,
            "materialize",
            "researcher",
            inp("pin_unavailable"),
            {"status": "rebind_failed", "code": "pin_unavailable"},
            {
                "researcher": [
                    "thread_started",
                    "user_input",
                    "turn_completed",
                    "member_ended",
                    SENT,
                    "mail_refused",
                    SENT,
                    "mail_refused",
                    SENT,
                ]
            },
        )
    )
    w = team()
    dispatch(w, "lead", "monitor", {"member": "researcher-1"}, "c3")
    out.append(
        Vec(
            "failed-rebind-pin-mismatch-fires-every-monitor",
            "4.10",
            "The rebuilt config_hash differs: the same end append, with one member_ended "
            "notification per monitor row on the member (the task monitor and the lead's end "
            "monitor), in monitor_id order.",
            w,
            "materialize",
            "researcher",
            inp("pin_mismatch"),
            {"status": "rebind_failed", "code": "pin_mismatch"},
            {
                "researcher": [
                    "thread_started",
                    "user_input",
                    "turn_completed",
                    "member_ended",
                    SENT,
                    SENT,
                ]
            },
        )
    )
    return out
