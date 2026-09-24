# pyright: strict
"""Team op vectors on the recipient's side (design §4.7 consume, §4.14 cancel's application):
batches, control mail, parks, and what a cancel releases."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .ops_send import send
from .team_ops_worlds import (
    NOW,
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
    return out + _park_vectors() + _cancel_applied() + _released() + _cancel_orders()


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


def _released() -> list[Vec]:
    """What one consume may not do, and what a cancel releases besides an ask."""
    w = _lead_idle_writer_parked()
    take(w, "lead")
    _writer_answered(w)
    lead = next(r for r in w.rows("team_members") if r["role"] == "lead")
    send(w, "team", operator(REQUESTS[3], {"to": w.ref(lead), "text": "Status?"}))
    settle = next(str(m["mail_id"]) for m in w.pending("lead") if m["kind"] == "member_settled")
    out = [
        _consume(
            "consume-unparked-mail-waits",
            "4.7",
            "The lead is parked on writer-1, whose settlement is pending, and so is an operator "
            "message of another request. The settlement is control mail: receipt and resumed, "
            "which opens the lead's turn under the lead's run. The operator's message is ordinary "
            "mail that the control row unblocked: it waits for the next consume, so one turn "
            "never holds two authorities.",
            w,
            "lead",
            _taken(settle),
            [RECEIVED, "resumed"],
        )
    ]
    w = _both()
    w.stamp = True
    writer_asks(w)
    w.now += 1000
    dispatch(w, "lead", "cancel", {"member": "writer-1"}, "c3")
    w.now += 1000
    take(w, "researcher")
    dispatch(w, "researcher", "reply", {"ask_id": ASK, "text": "Batteries."}, "c2")
    w.now += 1000
    out.append(
        Vec(
            "cancel-applied-asker-with-pending-reply",
            "4.8, 4.14",
            "The cancel mail is ahead of a reply the researcher sent while the ask was open. "
            "ask.complete's order still holds: the reply wins, so the ask closes answered, and "
            "the barrier follows the cancel's receipt.",
            w,
            "consume",
            "writer",
            {},
            _taken(f"{LEAD_BRANCH}:c3", f"{MEMBER_BRANCH}:c2"),
            {"writer": [RECEIVED, "cancel_requested", RECEIVED, *CLOSE]},
            w.now,
        )
    )
    w = _both()
    dispatch(w, "writer", "wait", {"members": ["researcher-1"]}, "c1")
    dispatch(w, "lead", "cancel", {"member": "writer-1"}, "c3")
    out.append(
        _consume(
            "cancel-applied-waiter",
            "4.12, 4.14",
            "writer-1 is parked on a wait when its cancel is applied: the receipt, the barrier, "
            "and the wait finishes with what has settled (nothing, so timed_out: the mode is "
            "unmet), which deletes its settle monitor row; resumed and the wait call's result "
            "follow.",
            w,
            "writer",
            _taken(f"{LEAD_BRANCH}:c3"),
            [RECEIVED, "cancel_requested", "wait_finished", "resumed", "tool_result"],
        )
    )
    return out


def _waiting_writer(*, settled_first: bool) -> World:
    """writer-1 waits on researcher-1; the lead's cancel and the researcher's settlement are
    both committed, in the given order, before the writer consumes."""
    w = _both()
    dispatch(w, "writer", "wait", {"members": ["researcher-1"]}, "c1")
    w.stamp = True
    w.now = NOW + 1000
    if settled_first:
        go_idle(w, "researcher", "Prices fell.")
        w.now += 1000
    dispatch(w, "lead", "cancel", {"member": "writer-1"}, "c3")
    if not settled_first:
        w.now += 1000
        go_idle(w, "researcher", "Prices fell.")
    w.now += 1000
    return w


def _cancel_orders() -> list[Vec]:
    out: list[Vec] = []
    for settled_first, name in ((False, "cancel-first"), (True, "settlement-first")):
        w = _waiting_writer(settled_first=settled_first)
        out.append(
            Vec(
                f"cancel-applied-waiter-counts-committed-settlement-{name}",
                "4.12, 4.14",
                "writer-1 waits on researcher-1. Its cancel and the researcher's settlement are "
                "both committed before the writer consumes"
                + (", the settlement first. " if settled_first else ", the cancel first. ")
                + "Either way the settlement counts (commit order, as at the deadline step): "
                "applying the cancel consumes the wait's committed notices, then finishes the "
                "wait with the researcher in finished and timed_out false.",
                w,
                "consume",
                "writer",
                {},
                _taken(*_mail_to(w, "writer-1")),
                {
                    "writer": [
                        RECEIVED,
                        "cancel_requested",
                        RECEIVED,
                        "wait_finished",
                        "resumed",
                        "tool_result",
                    ]
                    if not settled_first
                    else [
                        RECEIVED,
                        "wait_finished",
                        "resumed",
                        "tool_result",
                        RECEIVED,
                        "cancel_requested",
                    ]
                },
                w.now,
            )
        )
    w = _both(researcher_idle=True)
    w.stamp = True
    w.now = NOW + 1000
    dispatch(w, "lead", "cancel", {"member": "researcher-1"}, "c3")
    w.now += 1000
    dispatch(w, "lead", "send", {"to": "researcher-1", "text": "One more thing."}, "c4")
    w.now += 1000
    out.append(
        Vec(
            "cancel-stops-new-work",
            "4.7, 4.14",
            "The idle researcher's pending mail is the lead's cancel, then a message of the same "
            "run. The cancel is applied, and a cancelled member takes no new work: the message "
            "stays pending (the member's end refuses it), so no turn opens.",
            w,
            "consume",
            "researcher",
            {},
            _taken(f"{LEAD_BRANCH}:c3"),
            {"researcher": [RECEIVED, "cancel_requested"]},
            w.now,
        )
    )
    return out
