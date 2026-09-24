# pyright: strict
"""Team op vectors for asks (design §4.8 ask.open and ask.complete, §4.9 reply): opening, every
reply precondition, and every way an ask closes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .jcs import JsonValue
from .ops_consume import deadline
from .ops_send import ask
from .team_ops_worlds import (
    DUE,
    NOW,
    REQUESTS,
    Vec,
    dispatch,
    go_idle,
    operator,
    pending_call,
    refused,
    run,
    running,
    take,
    team,
    writer_asks,
)
from .team_pieces import LOG_BRANCH, MEMBER_BRANCH, RESEARCHER, WRITER_BRANCH

if TYPE_CHECKING:
    from .jcs import Obj
    from .ops_world import World

P, S, R = "message_policy_decided", "message_sent", "tool_result"
OP = "operator_request"


def ask_vectors() -> list[Vec]:
    question: Obj = {"to": "researcher-1", "question": "Which topic?"}
    ask_id = f"{WRITER_BRANCH}:c1"
    opened: Obj = {"status": "open", "ask_id": ask_id, "deadline": DUE}
    w = running(writer=True)
    inp = pending_call(w, "writer", "ask", question, "c1")
    out = [
        Vec(
            "ask-member-opens",
            "4.8",
            "writer-1 asks researcher-1: the grant, message_sent{ask} whose ask_id is its mail_id "
            "and whose deadline is now plus the default 120 s, then the park on the ask. It is "
            "the writer's first park while the lead's task monitor on it is unfired, so the same "
            "append sends the lead the one member_parked notice. The asks row is open.",
            w,
            "ask",
            "writer",
            inp,
            opened,
            {"writer": [P, S, "parked", S]},
        )
    ]
    w = running(writer=True)
    inp = pending_call(w, "writer", "ask", question, "c1")
    out.append(
        Vec(
            "ask-budget-exceeded",
            "4.8",
            "No headroom on a budget covering the recipient: refused budget_exceeded, after the "
            "send checks.",
            w,
            "ask",
            "writer",
            inp,
            refused("budget_exceeded"),
            {"writer": [P, R]},
            given={"headroom": False},
        )
    )
    body: Obj = {"to": RESEARCHER, "question": "Any risks?", "timeout_ms": 60_000}
    out.append(
        Vec(
            "ask-operator-opens",
            "4.4, 4.8",
            "team.ask with a 60 s timeout: operator_request, the grant and message_sent{ask} "
            "with deadline now + 60 s. The team log parks nothing.",
            running(),
            "ask",
            "team",
            operator(REQUESTS[0], body),
            {"status": "open", "ask_id": f"{LOG_BRANCH}:{REQUESTS[0]}", "deadline": NOW + 60_000},
            {"team": [OP, P, S]},
        )
    )
    return out + reply_vectors(ask_id) + _ask_complete_vectors()


def asked() -> World:
    """writer-1 asked researcher-1, which received the ask in its task turn."""
    w = running(writer=True)
    writer_asks(w)
    take(w, "researcher")
    return w


def replied() -> World:
    w = asked()
    dispatch(
        w, "researcher", "reply", {"ask_id": f"{WRITER_BRANCH}:c1", "text": "Batteries."}, "c2"
    )
    return w


def reply_vectors(ask_id: str) -> list[Vec]:
    answer: Obj = {"ask_id": ask_id, "text": "Batteries."}
    w = asked()
    out = [
        Vec(
            "reply-sent",
            "4.9",
            "The researcher replies to the ask it received: message_sent{reply} to the asker, "
            "with the ask's provenance, and the call's result. No policy decision.",
            w,
            "reply",
            "researcher",
            pending_call(w, "researcher", "reply", answer, "c2"),
            {"id": f"{MEMBER_BRANCH}:c2", "status": "sent"},
            {"researcher": [S, R]},
        )
    ]
    for name, world, args, code, now, desc in (
        (
            "reply-unknown-ask-refused",
            asked(),
            {**answer, "ask_id": f"{WRITER_BRANCH}:c9"},
            "unknown_ask",
            NOW,
            "No ask with that id reached this log.",
        ),
        (
            "reply-already-replied-refused",
            replied(),
            answer,
            "already_replied",
            NOW,
            "This log already replied to the ask.",
        ),
        (
            "reply-past-deadline-refused",
            asked(),
            answer,
            "ask_closed",
            DUE,
            "The asks row is still open, but now is its deadline: refused ask_closed.",
        ),
        (
            "reply-ask-closed-refused",
            _timed_out(),
            answer,
            "ask_closed",
            NOW + 150_000,
            "The asker's deadline step already closed the ask timed_out.",
        ),
    ):
        inp = pending_call(world, "researcher", "reply", args, "c3")
        out.append(
            Vec(
                name,
                "4.9",
                f"{desc} The refusal is the call's result; nothing is sent.",
                world,
                "reply",
                "researcher",
                inp,
                refused(code),
                {"researcher": [R]},
                now,
            )
        )
    return out


def _timed_out() -> World:
    w = asked()
    w.now = DUE
    deadline(w, "writer", {"id": f"{WRITER_BRANCH}:c1"})
    return w


RECEIVED = "message_received"
CLOSE = ["ask_closed", "resumed", "tool_result"]
ASK = f"{WRITER_BRANCH}:c1"


def _taking(name: str, desc: str, w: World, by: str, mail_ids: list[str]) -> Vec:
    """A consume that closes an ask; the team log appends no resumed and no result."""
    outcome: Obj = {"status": "consumed", "mail_ids": list[JsonValue](mail_ids)}
    types = [RECEIVED, "ask_closed"] if by == "team" else [RECEIVED, *CLOSE]
    return Vec(name, "4.8", desc, w, "consume", by, {}, outcome, {by: types})


def _idle_researcher() -> World:
    w = running(writer=True)
    go_idle(w, "researcher", "Battery prices fell.")
    return w


def _mail_to(w: World, name: str) -> list[str]:
    return [str(m["mail_id"]) for m in w.pending(name)]


def _ask_complete_vectors() -> list[Vec]:
    w = replied()
    reply_id = f"{MEMBER_BRANCH}:c2"
    out = [
        _taking(
            "ask-reply-closes-ask",
            "The parked writer takes the reply as control mail: message_received, ask_closed "
            "{answered}, resumed and the ask call's one result, in one append.",
            w,
            "writer",
            [reply_id],
        )
    ]
    run_failed = team(writer=True)
    run(run_failed, "writer-1")
    writer_asks(run_failed)
    run(run_failed, rebind="pin_unavailable")
    bounce = _mail_to(run_failed, "writer-1")
    out.append(
        _taking(
            "ask-bounce-closes-member-ended",
            "The writer asked the starting researcher, whose rebind then failed: the ask's bounce "
            "carries its ask_id and the result, and the parked writer closes the ask member_ended.",
            run_failed,
            "writer",
            bounce,
        )
    )
    timed: Obj = {"ask_id": ASK, "status": "timed_out"}
    out.append(
        Vec(
            "ask-deadline-timed-out",
            "4.8",
            "The team worker's deadline step at the deadline, with no reply or bounce pending: "
            "ask_closed{timed_out}, resumed and the call's result.",
            asked(),
            "deadline",
            "writer",
            {"id": ASK},
            timed,
            {"writer": CLOSE},
            DUE,
        )
    )
    answered: Obj = {
        "ask_id": ASK,
        "member": RESEARCHER,
        "status": "answered",
        "text": "Batteries.",
    }
    out.append(
        Vec(
            "ask-deadline-reply-wins",
            "4.8",
            "At the deadline a reply is pending: it committed while the ask was open and before "
            "the "
            "deadline, so the step answers, never times out.",
            replied(),
            "deadline",
            "writer",
            {"id": ASK},
            answered,
            {"writer": [RECEIVED, *CLOSE]},
            DUE,
        )
    )
    out.append(
        Vec(
            "ask-deadline-not-due",
            "4.8",
            "Before the deadline the step does nothing.",
            asked(),
            "deadline",
            "writer",
            {"id": ASK},
            {"status": "not_due"},
            {},
        )
    )
    return out + _operator_ask()


def _operator_ask() -> list[Vec]:
    w = _idle_researcher()
    ask(w, "team", operator(REQUESTS[1], {"to": RESEARCHER, "question": "Any risks?"}))
    take(w, "researcher")
    ask_id = f"{LOG_BRANCH}:{REQUESTS[1]}"
    dispatch(w, "researcher", "reply", {"ask_id": ask_id, "text": "None."}, "c2")
    return [
        _taking(
            "ask-operator-reply-closes-ask",
            "team.ask's reply reaches the team log, which takes everything: message_received and "
            "ask_closed{answered}; no resumed and no tool_result (team.ask returns it).",
            w,
            "team",
            [f"{MEMBER_BRANCH}:c2"],
        )
    ]
