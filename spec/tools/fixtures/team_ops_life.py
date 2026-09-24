# pyright: strict
"""Team op vectors for a member's own settling appends (design §4.11: idle, end, lead close,
the tie rule's losing settlement) and for the team log as a recipient (an operator start's park
notice and settlement, a bounce to the operator, an operator wait's settle mail)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import text, tokens
from .ops_consume import deadline
from .ops_life import end
from .ops_member import start
from .ops_observe import wait
from .ops_send import send
from .team_ops_worlds import (
    DUE,
    NOW,
    REQUESTS,
    Vec,
    dispatch,
    go_idle,
    operator,
    run,
    running,
    take,
    team,
)
from .team_pieces import RESEARCHER, WRITER_THREAD

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj
    from .ops_world import World

S, RECEIVED = "message_sent", "message_received"
DOWN: Obj = {
    "reason": "model_unavailable",
    "result": {"status": "failed", "error": {"code": "model_unavailable", "message": "Down."}},
}


def _answers(w: World, label: str, said: str) -> None:
    """The member's model answered end_turn; the turn is not closed yet."""
    log = w.logs[label]
    r = log.model_request()
    log.model_response(r, [{"type": "text", "text": said}], "end_turn", tokens(90, 10))


def _idle(name: str, desc: str, w: World, types: list[str], now: int = NOW) -> Vec:
    _answers(w, "researcher", "Prices fell.")
    result: Obj = {"member": RESEARCHER, "status": "completed", "output": {"text": "Prices fell."}}
    outcome: Obj = {"status": "idle", "result": result}
    types = ["turn_completed", "member_idle", *types]
    return Vec(name, "4.11", desc, w, "idle", "researcher", {}, outcome, {"researcher": types}, now)


def life_vectors() -> list[Vec]:
    w = running()
    dispatch(w, "lead", "wait", {"members": ["researcher-1"]}, "c3")
    out = [
        _idle(
            "idle-fires-settle-and-task-monitors",
            "The researcher's task turn ends completed: turn_completed, member_idle with the "
            "answer, then member_settled for each settle monitor and its unfired task monitor, in "
            "monitor_id order, deleting both rows; the row becomes idle with its result.",
            w,
            [S, S],
        )
    ]
    w = running()
    dispatch(w, "lead", "wait", {"members": ["researcher-1"]}, "c3")
    w.now = DUE
    deadline(w, "lead", {"id": "0192b000-0000-7000-8000-0000000000b1:c3"})
    out.append(
        _idle(
            "idle-after-wait-finished-sends-no-settle",
            "The tie rule's loser: the lead's wait finished at its deadline first and deleted its "
            "settle row, so the researcher's later idle append sends only the task notification.",
            w,
            [S],
            DUE,
        )
    )
    w = running(writer=True)
    dispatch(w, "lead", "send", {"to": "researcher-1", "text": "Keep it short."}, "c3")
    dispatch(w, "writer", "ask", {"to": "researcher-1", "question": "Which topic?"}, "c1")
    w.logs["researcher"].model_request()
    out.append(
        Vec(
            "end-member-refuses-pending-mail",
            "4.11",
            "The researcher's model fails: turn_completed, member_ended{failed}, the task "
            "monitor's member_ended, then a mail_refused and a bounce for each pending mail (the "
            "writer's ask's bounce carries its ask_id and the result).",
            w,
            "end",
            "researcher",
            DOWN,
            {"status": "ended"},
            {
                "researcher": [
                    "turn_completed",
                    "member_ended",
                    S,
                    "mail_refused",
                    S,
                    "mail_refused",
                    S,
                ]
            },
        )
    )
    w = running(writer=True)
    w.logs["lead"].model_request()
    out.append(
        Vec(
            "end-lead-closes-team",
            "4.11",
            "The lead's model fails: its member_ended sets teams.closed_at, and the same append "
            "sends one cancel to each live member (running researcher-1, running writer-1), in "
            "name order, with the lead's run as provenance.",
            w,
            "end",
            "lead",
            DOWN,
            {"status": "ended"},
            {"lead": ["turn_completed", "member_ended", S, S]},
        )
    )
    return [*out, _ended_lead_refuses(), *_team_log_vectors()]


def _operator_started() -> World:
    """team.start started writer-1, which is running."""
    w = running()
    start(
        w,
        "team",
        {
            **operator(REQUESTS[1], {"agent": "writer", "task": "Draft."}),
            "thread_id": WRITER_THREAD,
        },
    )
    run(w, "writer-1")
    return w


def _team_takes(name: str, desc: str, w: World, types: list[str]) -> Vec:
    ids: list[JsonValue] = [m["mail_id"] for m in w.pending("team_log")]
    outcome: Obj = {"status": "consumed", "mail_ids": ids}
    return Vec(name, "4.3, 4.7", desc, w, "consume", "team", {}, outcome, {"team": types})


def _team_log_vectors() -> list[Vec]:
    w = _operator_started()
    dispatch(w, "writer", "ask", {"to": "researcher-1", "question": "Which topic?"}, "c1")
    out = [
        _team_takes(
            "team-log-takes-park-notice",
            "writer-1, started by the operator, parks on an ask; its member_parked notice goes to "
            "the team log, which records the receipt only: no parked, no turn.",
            w,
            [RECEIVED],
        )
    ]
    w = _operator_started()
    go_idle(w, "writer", "Drafted.")
    out.append(
        _team_takes(
            "team-log-takes-settlement",
            "writer-1's settlement reaches the team log as its task notification: receipt only, "
            "no resumed and no turn.",
            w,
            [RECEIVED],
        )
    )
    w = team()
    send(w, "team", operator(REQUESTS[1], {"to": RESEARCHER, "text": "Status?"}))
    run(w, rebind="pin_unavailable")
    out.append(
        _team_takes(
            "team-log-takes-bounce",
            "The operator's message to a starting member is returned when its rebind fails: the "
            "bounce reaches the team log, which records it.",
            w,
            [RECEIVED],
        )
    )
    w = running()
    wait(w, "team", operator(REQUESTS[1], {"members": [RESEARCHER]}))
    go_idle(w, "researcher", "Prices fell.")
    out.append(
        _team_takes(
            "team-log-wait-finishes",
            "The operator waits for the running researcher; its idle append sends the settle mail "
            "to the team log, whose consume records it and finishes the wait (no resumed, no "
            "tool_result: team.wait returns it). The lead's task notification is not the team "
            "log's.",
            w,
            [RECEIVED, "wait_finished"],
        )
    )
    return out


def _ended_lead_refuses() -> Vec:
    """After lead close, a member applies its cancel and ends: its end notice reaches the
    lead's task monitor, but the lead has ended."""
    w = running()
    w.logs["lead"].model_request()
    end(w, "lead", DOWN)
    take(w, "researcher")
    ended_: Obj = {"reason": "cancelled", "result": {"status": "cancelled"}}
    end(w, "researcher", ended_)
    notice = [text(m["mail_id"]) for m in w.pending("lead")]
    return Vec(
        "ended-lead-refuses-late-notice",
        "4.11",
        "The lead's end closed the team and cancelled researcher-1, whose own end then fired "
        "the lead's task monitor. Mail reaching a member that already ended is refused under its "
        "writer, never left pending: mail_refused{member_ended}, with no bounce for a "
        "notification (only a message or an ask is bounced).",
        w,
        "consume",
        "lead",
        {},
        {"status": "refused", "mail_ids": [*notice]},
        {"lead": ["mail_refused"]},
    )
