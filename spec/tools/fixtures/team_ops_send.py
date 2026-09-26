# pyright: strict
"""Team op vectors that send (design §4.5 send, §4.14 cancel's request): every precondition
and outcome, for model calls and operator requests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import eid
from .team_ops_worlds import (
    GLOBEX,
    REQUESTS,
    Vec,
    ended,
    operator,
    pending_call,
    refused,
    running,
    team,
)
from .team_pieces import (
    LEAD,
    LEAD_BRANCH,
    LOG_BRANCH,
    MEMBER_BRANCH,
    RESEARCHER,
    Route,
    at,
    envelope,
    provenance,
)

if TYPE_CHECKING:
    from .jcs import Obj
    from .ops_world import World

P, S, R = "message_policy_decided", "message_sent", "tool_result"
OP = "operator_request"


def _lead_send(name: str, to: str, w: World, outcome: Obj, desc: str, **given: int) -> Vec:
    args: Obj = {"to": to, "text": "Keep it short."}
    inp = pending_call(w, "lead", "send", args, "c3")
    types = [P, S, R] if outcome["status"] == "sent" else [P, R]
    return Vec(
        name, "4.5", desc, w, "send", "lead", inp, outcome, {"lead": types}, given=dict(given)
    )


def send_vectors() -> list[Vec]:
    lead_mail = f"{LEAD_BRANCH}:c3"
    out = [
        _lead_send(
            "send-lead-to-member",
            "researcher-1",
            running(),
            {"id": lead_mail, "status": "sent"},
            "The lead sends a running member a message: the team's grant, message_sent (kind "
            "message, to researcher-1 at generation 1, the lead's run as provenance, the call as "
            "causal) and the call's result; the mail row is pending.",
        ),
        _lead_send(
            "send-unknown-member-refused",
            "writer-7",
            running(),
            refused("unknown_member"),
            "No member has that name: the decision is logged, then the refusal as the result.",
        ),
        _lead_send(
            "send-self-refused",
            "lead",
            running(),
            refused("self"),
            "The lead addresses itself: refused self.",
        ),
        _lead_send(
            "send-member-ended-refused",
            "researcher-1",
            ended(),
            refused("member_ended"),
            "The member ended (its rebind failed): refused member_ended.",
        ),
        _lead_send(
            "send-mailbox-full-refused",
            "researcher-1",
            team(),
            refused("mailbox_full"),
            "A starting member's pending task already fills a mailbox of 1: refused mailbox_full.",
            mailbox=1,
        ),
    ]
    w = running()
    inp = pending_call(w, "researcher", "send", {"to": "lead", "text": "Prices fell."}, "c1")
    out.append(
        Vec(
            "send-member-to-lead",
            "4.5",
            "A member sends the lead a message; its provenance is its task's (the lead's run).",
            w,
            "send",
            "researcher",
            inp,
            {"id": f"{MEMBER_BRANCH}:c1", "status": "sent"},
            {"researcher": [P, S, R]},
        )
    )
    out += [_closed(), *_operator_sends()]
    return out


def _closed() -> Vec:
    """The lead ended: its terminal append closes the team and cancels every live member."""
    w = running()
    lead = w.logs["lead"]
    lead.add("turn_completed", {"reason": "model_unavailable"})
    ended: Obj = {
        "member": LEAD,
        "status": "failed",
        "error": {"code": "model_unavailable", "message": "The model is unavailable."},
    }
    end = lead.add("member_ended", {"result": ended})
    root = str(lead.events[1]["event_id"])
    note = envelope(
        f"{LEAD_BRANCH}:{eid(lead.seq + 1, LEAD_BRANCH)}",
        "cancel",
        Route(LEAD, "researcher-1", provenance(root)),
        at(str(end["event_id"])),
    )
    lead.add("message_sent", {"envelope": note})
    inp = pending_call(w, "researcher", "send", {"to": "lead", "text": "Done."}, "c1")
    return Vec(
        "send-team-closed-refused",
        "4.5",
        "The lead ended, which closed the team (teams.closed_at) and sent the researcher a "
        "cancel it hasn't applied yet; the researcher's send is refused team_closed.",
        w,
        "send",
        "researcher",
        inp,
        refused("team_closed"),
        {"researcher": [P, R]},
    )


def _operator_sends() -> list[Vec]:
    body: Obj = {"to": RESEARCHER, "text": "Status?"}
    return [
        Vec(
            "send-operator",
            "4.4, 4.5",
            "team.send with an idempotency key: operator_request (its own event is the root "
            "request), the team's grant, and message_sent from {operator: request_id}; the mail "
            "row and the operator_receipts row are inserted.",
            running(),
            "send",
            "team",
            operator(REQUESTS[0], body, "send-1"),
            {"id": f"{LOG_BRANCH}:{REQUESTS[0]}", "status": "sent"},
            {"team": [OP, P, S]},
        ),
        Vec(
            "send-operator-member-ended-refused",
            "4.4, 4.5",
            "team.send to an ended member: the request, the grant and operator_refused.",
            ended(),
            "send",
            "team",
            operator(REQUESTS[0], body),
            refused("member_ended"),
            {"team": [OP, P, "operator_refused"]},
        ),
        Vec(
            "send-operator-other-tenant-forbidden",
            "4.4, 4.5",
            "An operator acting for a principal of another tenant: default deny, logged, then "
            "operator_refused{forbidden}.",
            running(),
            "send",
            "team",
            operator(REQUESTS[0], body, who=GLOBEX),
            refused("forbidden"),
            {"team": [OP, P, "operator_refused"]},
        ),
        Vec(
            "send-operator-foreign-tenant-ref",
            "4.4, 4.5",
            "A MemberRef carrying another tenant with this team's id names no member here: "
            "refused unknown_member. A ref is read whole, tenant included.",
            running(),
            "send",
            "team",
            operator(REQUESTS[0], {**body, "to": {**RESEARCHER, "tenant": "globex"}}),
            refused("unknown_member"),
            {"team": [OP, P, "operator_refused"]},
        ),
    ]


def cancel_vectors() -> list[Vec]:
    out: list[Vec] = []
    for name, world, by, code, desc in (
        (
            "cancel-requested",
            running(),
            "lead",
            None,
            "The lead cancels the member it started: the grant, message_sent{cancel} and "
            "cancel_requested as the result. Durable, not yet applied.",
        ),
        (
            "cancel-not-starter-forbidden",
            running(writer=True),
            "writer",
            "forbidden",
            "writer-1 did not start researcher-1: default deny, logged, and refused forbidden.",
        ),
        (
            "cancel-member-ended-refused",
            ended(),
            "lead",
            "member_ended",
            "The member already ended: refused member_ended.",
        ),
    ):
        inp = pending_call(world, by, "cancel", {"member": "researcher-1"}, "c3")
        requested: Obj = {"member": RESEARCHER, "status": "cancel_requested"}
        outcome = requested if code is None else refused(code)
        types = [P, S, R] if code is None else [P, R]
        out.append(Vec(name, "4.14", desc, world, "cancel", by, inp, outcome, {by: types}))
    out.append(
        Vec(
            "cancel-operator",
            "4.4, 4.14",
            "team.cancel: operator_request, the grant and message_sent{cancel}.",
            running(),
            "cancel",
            "team",
            operator(REQUESTS[0], {"member": RESEARCHER}),
            {"member": RESEARCHER, "status": "cancel_requested"},
            {"team": [OP, P, S]},
        )
    )
    return out
