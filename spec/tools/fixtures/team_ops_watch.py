# pyright: strict
"""Team op vectors that watch members (design §4.12 wait, §4.13 monitor): registration, members
already settled, the deadline step and its tie rule, and wait mode n above the member count."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .ops_world import public
from .team_ops_worlds import (
    DUE,
    REQUESTS,
    Vec,
    dispatch,
    go_idle,
    operator,
    pending_call,
    refused,
    run,
    team,
    writer_asks,
)
from .team_pieces import LEAD_BRANCH, LOG_BRANCH, RESEARCHER, WRITER, failed

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj
    from .ops_world import World

P, R = "message_policy_decided", "tool_result"
WAIT = f"{LEAD_BRANCH}:c3"
PRICES: Obj = {"member": RESEARCHER, "status": "completed", "output": {"text": "Prices fell."}}


def _both(*, idle: bool = False) -> World:
    w = team(writer=True)
    run(w)
    run(w, "writer-1")
    if idle:
        go_idle(w, "researcher", "Prices fell.")
    return w


def _waited(finished: list[JsonValue], pending: list[JsonValue], timed_out: bool) -> Obj:
    return {
        "status": "waited",
        "finished": [public(r) for r in finished],
        "parked": [],
        "pending": pending,
        "timed_out": timed_out,
    }


def _lead_waits(w: World, members: list[JsonValue]) -> Obj:
    return pending_call(w, "lead", "wait", {"members": members}, "c3")


def wait_vectors() -> list[Vec]:
    w = _both()
    out = [
        Vec(
            "wait-registers-monitor",
            "4.12",
            "The lead waits for a running member: the grant (wait is decided as monitor), "
            "wait_started (mode all, deadline now + 120 s) with a settle monitor row, and the "
            "park on the wait.",
            w,
            "wait",
            "lead",
            _lead_waits(w, ["researcher-1"]),
            {"status": "waiting", "wait_id": WAIT},
            {"lead": [P, "wait_started", "parked"]},
        )
    ]
    w = _both(idle=True)
    out.append(
        Vec(
            "wait-already-settled",
            "4.12",
            "The researcher is already idle and is listed twice: the repeat is dropped before "
            "anything is recorded, wait_started lists it once, member_observed copies its "
            "committed "
            "member_idle result (source is that event), and the mode is met, so wait_finished and "
            "the call's result are in the same append. No park, no monitor row left.",
            w,
            "wait",
            "lead",
            _lead_waits(w, ["researcher-1", "researcher-1"]),
            _waited([PRICES], [], timed_out=False),
            {"lead": [P, "wait_started", "member_observed", "wait_finished", R]},
        )
    )
    w = _both()
    out.append(
        Vec(
            "wait-unknown-member-refused",
            "4.12",
            "A listed name is not a member: refused unknown_member before the policy is asked, "
            "recorded as the call's result.",
            w,
            "wait",
            "lead",
            _lead_waits(w, ["researcher-1", "writer-9"]),
            refused("unknown_member"),
            {"lead": [R]},
        )
    )
    return out + _deadlines() + _operator_waits()


def _registered() -> World:
    w = _both()
    dispatch(w, "lead", "wait", {"members": ["researcher-1"]}, "c3")
    return w


def _deadlines() -> list[Vec]:
    out = [
        Vec(
            "wait-deadline-timed-out",
            "4.12",
            "The deadline step with the member still running: wait_finished{timed_out} lists it "
            "pending and deletes the monitor row; resumed and the call's result follow.",
            _registered(),
            "deadline",
            "lead",
            {"id": WAIT},
            _waited([], [RESEARCHER], timed_out=True),
            {"lead": ["wait_finished", "resumed", R]},
            DUE,
        )
    ]
    w = _registered()
    go_idle(w, "researcher", "Prices fell.")
    out.append(
        Vec(
            "wait-deadline-counts-committed-settlement",
            "4.12",
            "The tie rule: the researcher's idle append deleted the settle monitor row and sent "
            "its "
            "notification before the deadline step ran, so the step consumes it and the member "
            "counts; the mode is met and timed_out is false. Commit order, never timestamps.",
            w,
            "deadline",
            "lead",
            {"id": WAIT},
            _waited([PRICES], [], timed_out=False),
            {"lead": ["message_received", "wait_finished", "resumed", R]},
            DUE,
        )
    )
    return out


def _operator_waits() -> list[Vec]:
    both: list[JsonValue] = [RESEARCHER, WRITER]
    out = [
        Vec(
            "wait-operator-n-over-count-invalid",
            "4.12",
            "team.wait with mode 3 on two members is refused invalid_request before any writer "
            "is acquired: nothing is recorded, no request exists, and no row changes.",
            _both(),
            "wait",
            "team",
            operator(REQUESTS[0], {"members": both, "mode": 3}, "wait-1"),
            refused("invalid_request"),
            {},
        ),
        Vec(
            "wait-operator-observed",
            "4.12",
            "team.wait on an idle member: operator_request, the grant, wait_started, "
            "member_observed and wait_finished in one append. The team log parks nothing.",
            _both(idle=True),
            "wait",
            "team",
            operator(REQUESTS[0], {"members": [RESEARCHER]}),
            _waited([PRICES], [], timed_out=False),
            {"team": ["operator_request", P, "wait_started", "member_observed", "wait_finished"]},
        ),
        Vec(
            "wait-operator-any",
            "4.12",
            "team.wait mode any: the idle researcher is observed and meets the mode, so the wait "
            "finishes at once with the writer pending; any never cancels the others.",
            _both(idle=True),
            "wait",
            "team",
            operator(REQUESTS[0], {"members": both, "mode": "any"}),
            _waited([PRICES], [WRITER], timed_out=False),
            {
                "team": [
                    "operator_request",
                    P,
                    P,
                    "wait_started",
                    "member_observed",
                    "wait_finished",
                ]
            },
        ),
    ]
    w = _both()
    writer_asks(w)
    from .ops_observe import wait  # noqa: PLC0415

    wait(w, "team", operator(REQUESTS[1], {"members": [WRITER]}))
    parked: Obj = {
        **_waited([], [], timed_out=True),
        "parked": [{"member": WRITER, "reason": "awaiting_member"}],
    }
    out.append(
        Vec(
            "wait-deadline-lists-parked",
            "4.12",
            "The operator's wait on writer-1 reaches its deadline while the writer is parked on "
            "its "
            "ask: a parked member does not settle, so wait_finished lists it under parked and "
            "times "
            "out. The team log writes only wait_finished.",
            w,
            "deadline",
            "team",
            {"id": f"{LOG_BRANCH}:{REQUESTS[1]}"},
            parked,
            {"team": ["wait_finished"]},
            DUE,
        )
    )
    return out


def monitor_vectors() -> list[Vec]:
    out: list[Vec] = []
    ended = team()
    run(ended, rebind="pin_unavailable")
    monitoring: Obj = {"member": RESEARCHER, "status": "monitoring"}
    gone: Obj = {"result": failed(RESEARCHER, "pin_unavailable"), "status": "ended"}
    for name, w, target, outcome, types, desc in (
        (
            "monitor-running",
            _both(),
            "researcher-1",
            monitoring,
            [P, "monitor_set", R],
            "The lead monitors a running member: the grant, monitor_set and an end monitor row.",
        ),
        (
            "monitor-ended",
            ended,
            "researcher-1",
            gone,
            [P, "monitor_set", "member_observed", R],
            "The target already ended: monitor_set, then member_observed with its member_ended "
            "result, and the result says ended; no row.",
        ),
        (
            "monitor-unknown-member-refused",
            _both(),
            "writer-9",
            refused("unknown_member"),
            [P, R],
            "The grant is decided first, then the target is looked up: refused unknown_member.",
        ),
    ):
        inp = pending_call(w, "lead", "monitor", {"member": target}, "c3")
        out.append(Vec(name, "4.13", desc, w, "monitor", "lead", inp, outcome, {"lead": types}))
    return out
