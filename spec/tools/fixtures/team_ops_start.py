# pyright: strict
"""Team op vectors for a member's start (design §4.10): member.start from the lead and from the
operator, every refusal in check order, naming after an ended member, materialize, and the failed
rebind's one end append with every kind of queued mail and monitor."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .ops_life import end
from .ops_member import start
from .team_ops_worlds import (
    BOB,
    REQUESTS,
    Vec,
    dispatch,
    ended,
    operator,
    pending_call,
    refused,
    run,
    running,
    team,
    writer_asks,
)
from .team_pieces import MEMBER_BRANCH, WRITER, WRITER_BRANCH, WRITER_THREAD, ref

if TYPE_CHECKING:
    from .jcs import Obj
    from .ops_world import World

P, S, R = "message_policy_decided", "message_sent", "tool_result"
NEW_THREAD = "0192a000-0000-7000-8000-0000000000b5"
REBUILT = ["thread_started", "user_input", "turn_completed", "member_ended"]


def _start(w: World, by: str, agent: str = "researcher") -> Obj:
    inp = pending_call(w, by, "start", {"agent": agent, "task": "Check the sources."}, "c9")
    return {**inp, "thread_id": NEW_THREAD}


def start_vectors() -> list[Vec]:
    second = ref("researcher-2")
    out: list[Vec] = []
    cases: list[tuple[str, World, str, Obj, Obj, str]] = [
        (
            "start-lead-starts",
            running(),
            "lead",
            {"member": second, "status": "started"},
            {},
            "The lead starts a second researcher: its name is researcher-2 (k counts that agent's "
            "members, read in the transaction). member_started (its parent is itself, through the "
            "lead) and the task's message_sent insert the starting row, the pending task and the "
            "lead's task monitor; the call's result says started.",
        ),
        (
            "start-names-after-ended",
            ended(),
            "lead",
            {"member": second, "status": "started"},
            {},
            "researcher-1 ended; a new start is still researcher-2: an ended member keeps its "
            "name.",
        ),
        (
            "start-member-forbidden",
            running(writer=True),
            "writer",
            refused("forbidden"),
            {},
            "Only a lead starts: writer-1's start is denied by default, logged, and refused.",
        ),
        (
            "start-unknown-agent",
            running(),
            "lead",
            refused("unknown_agent"),
            {},
            "The team lists no such agent.",
        ),
        (
            "start-concurrency-cap",
            running(),
            "lead",
            refused("concurrency_cap"),
            {"concurrent": 1},
            "teamLimits.concurrent is 1 and researcher-1 is running: starting and running members "
            "count toward the cap, idle and ended ones don't.",
        ),
        (
            "start-budget-exceeded",
            running(),
            "lead",
            refused("budget_exceeded"),
            {"headroom": False},
            "No headroom for one request of the new member's model.",
        ),
    ]
    for name, world, by, outcome, given, desc in cases:
        agent = "editor" if name == "start-unknown-agent" else "researcher"
        types = [P, "member_started", S, R] if outcome["status"] == "started" else [P, R]
        inp = _start(world, by, agent)
        out.append(
            Vec(name, "4.10", desc, world, "start", by, inp, outcome, {by: types}, given=given)
        )
    out += [_closed(), _operator()]
    return out


def _closed() -> Vec:
    w = running()
    lead_end: Obj = {
        "reason": "model_unavailable",
        "result": {"status": "failed", "error": {"code": "model_unavailable", "message": "Down."}},
    }
    w.logs["lead"].model_request()
    end(w, "lead", lead_end)
    return Vec(
        "start-team-closed",
        "4.10",
        "The lead ended (a model failure), which closed the team. An operator start is refused "
        "team_closed after the team's grant.",
        w,
        "start",
        "team",
        {**operator(REQUESTS[0], {"agent": "writer", "task": "Go."}), "thread_id": NEW_THREAD},
        refused("team_closed"),
        {"team": ["operator_request", P, "operator_refused"]},
    )


def _operator() -> Vec:
    body: Obj = {"agent": "writer", "task": "Draft the summary."}
    return Vec(
        "start-operator",
        "4.4, 4.10",
        "team.start: operator_request, the grant, member_started in the team log (its parent is "
        "the lead's thread_started, which carries the team; its provenance is the request) and "
        "the task from {operator: request_id}. The task monitor's watcher is the team log.",
        running(),
        "start",
        "team",
        {**operator(REQUESTS[0], body, "start-1"), "thread_id": NEW_THREAD},
        {"member": WRITER, "status": "started"},
        {"team": ["operator_request", P, "member_started", S]},
    )


def _inp(rebind: str, name: str = "researcher-1") -> Obj:
    label, branch = (
        ("writer", WRITER_BRANCH) if name == "writer-1" else ("researcher", MEMBER_BRANCH)
    )
    return {"member": name, "label": label, "branch_id": branch, "rebind": rebind}


def materialize_vectors() -> list[Vec]:
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
            _inp("ok"),
            {"status": "materialized"},
            {"researcher": ["thread_started", "user_input"]},
        ),
        Vec(
            "materialize-not-starting",
            "4.10",
            "The row check in the transaction: the member already has its branch and is no "
            "longer starting, so nothing is committed. This is also why branch.open's "
            "already_open never follows a passed row check.",
            running(),
            "materialize",
            "researcher",
            _inp("ok"),
            {"status": "not_starting"},
            {},
        ),
    ]
    w = team(writer=True)
    run(w, "writer-1")
    dispatch(w, "lead", "send", {"to": "researcher-1", "text": "Keep it short."}, "c3")
    writer_asks(w)
    out.append(
        Vec(
            "failed-rebind-pin-unavailable",
            "4.10",
            "The rebind finds the definition unregistered: one append opens the branch with "
            "thread_started, the task's user_input, turn_completed{error: pin_unavailable} and "
            "member_ended{failed}; one member_ended notification for the lead's task monitor; "
            "then a mail_refused and a bounce per pending mail in (created_at, mail_id) order. "
            "Each log has its own clock here, so the writer's ask comes before the lead's "
            "message. The ask's "
            "bounce carries the ask_id and the result; each bounce carries its refused mail's "
            "provenance.",
            w,
            "materialize",
            "researcher",
            _inp("pin_unavailable"),
            {"status": "rebind_failed", "code": "pin_unavailable"},
            {"researcher": [*REBUILT, S, "mail_refused", S, "mail_refused", S]},
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
            _inp("pin_mismatch"),
            {"status": "rebind_failed", "code": "pin_mismatch"},
            {"researcher": [*REBUILT, S, S]},
        )
    )
    return [*out, _queued_cancel_and_wait(), _bounce_keeps_its_request()]


def _queued_cancel_and_wait() -> Vec:
    w = team(writer=True)
    run(w, "writer-1")
    dispatch(w, "lead", "cancel", {"member": "researcher-1"}, "c3")
    dispatch(w, "writer", "wait", {"members": ["researcher-1"]}, "c1")
    return Vec(
        "failed-rebind-with-queued-cancel-and-wait",
        "4.10, 4.11",
        "While researcher-1 was starting, the lead requested its cancel and writer-1 waits on "
        "it. The failed rebind fires the lead's task monitor and the writer's settle monitor "
        "(member_ended, monitor_id order), and returns the queued cancel with a bounce.",
        w,
        "materialize",
        "researcher",
        _inp("pin_unavailable"),
        {"status": "rebind_failed", "code": "pin_unavailable"},
        {"researcher": [*REBUILT, S, S, "mail_refused", S]},
    )


def _bounce_keeps_its_request() -> Vec:
    """Bob's operator request started writer-1; Alice's lead run sent it a message."""
    w = running()
    start(
        w,
        "team",
        {
            **operator(REQUESTS[1], {"agent": "writer", "task": "Go."}, who=BOB),
            "thread_id": WRITER_THREAD,
        },
    )
    dispatch(w, "lead", "send", {"to": "writer-1", "text": "Use the new data."}, "c3")
    return Vec(
        "failed-rebind-bounce-keeps-its-request",
        "4.10",
        "Bob's operator request started writer-1, and Alice's lead run sent it a message. The "
        "rebind fails: the task notification (to the team log) carries Bob's provenance, but "
        "the bounce to the lead carries the message's, Alice's run, so it can't wake the lead "
        "under Bob's authority or budget root.",
        w,
        "materialize",
        "writer",
        _inp("pin_unavailable", "writer-1"),
        {"status": "rebind_failed", "code": "pin_unavailable"},
        {"writer": [*REBUILT, S, "mail_refused", S]},
    )
