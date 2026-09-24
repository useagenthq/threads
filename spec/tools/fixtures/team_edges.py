# pyright: strict
"""The edges of semantic rules 31-42 on one log (spec/schema/README.md, "Semantic rules"): each
rejected log's last event breaks the rule. Plus the team log's repair fork (rule 33) and a late
result recorded without a wake after the lead ended (rule 37, "Background wakes")."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, eid, text, tokens
from .pieces import answer, call, reduce_case, reject, render_case, result, user
from .team_pieces import (
    DEADLINE,
    LEAD,
    LEAD_BRANCH,
    LEAD_THREAD,
    LOG_BRANCH,
    LOG_THREAD,
    MEMBER_BRANCH,
    MEMBER_THREAD,
    REQUEST,
    RESEARCHER,
    TEAM_TOOLS,
    Route,
    at,
    body,
    completed,
    config_hash,
    envelope,
    failed,
    lead_log,
    member_log,
    provenance,
    team_log,
)
from .team_steps import FAM, host, received

if TYPE_CHECKING:
    import pathlib

    from .jcs import JsonValue, Obj
    from .log import Log

KID = "0192a000-0000-7000-8000-0000000000cc"
REPAIR_BRANCH = "0192b000-0000-7000-8000-0000000000b5"


def build(root: pathlib.Path) -> None:
    for name, desc, log in (*_mail_cases(), *_ended_cases(), *_operator_cases()):
        reject(root, (name, FAM, desc), log)
    reduce_case(
        root,
        (
            "legacy-late-result-after-lead-ended-no-wake",
            FAM,
            "Rule 37's accepted twin: after the lead's member_ended, its background child's late "
            "result is recorded without a woken, and the log opens no turn.",
        ),
        _ended_with_child(),
        {},
    )
    render_case(
        root,
        (
            "render-team-message-lines",
            FAM,
            "Render v1 message lines: operator mail (escaped), an ask with a ref body, a bounce "
            "naming no ask, a task notice with a ref output and an end monitor's member_ended "
            "each open a turn and render; a member_parked notice renders nothing.",
        ),
        _mail_lines(),
    )
    repaired = team_log().fork(1, REPAIR_BRANCH, None, 1)
    reduce_case(
        root,
        (
            "team-log-repair-fork",
            FAM,
            "Rule 33: a team log's repair fork is admitted; the child is inspection-only.",
        ),
        repaired,
        {},
    )


def _note(mail_id: str, root_event: str, kind: str = "message", **more: JsonValue) -> Obj:
    route = Route(RESEARCHER, "lead", provenance(root_event))
    return envelope(mail_id, kind, route, at(root_event), **more)


def _mail_cases() -> list[tuple[str, str, Log]]:
    out: list[tuple[str, str, Log]] = []

    log = lead_log()
    root = text(user(log, "Watch the prices.")["event_id"])
    received(log, _note(f"{MEMBER_BRANCH}:c1", root, body=body("Prices fell.")))
    host(log, "mail_refused", {"mail_id": f"{MEMBER_BRANCH}:c1", "code": "stale_member"})
    out.append(
        (
            "team-mail-refused-after-receipt-rejected",
            "Rule 31: a mail already received is refused.",
            log,
        )
    )

    log = member_log(eid(4, LEAD_BRANCH))
    host(log, "mail_refused", {"mail_id": f"{LEAD_BRANCH}:c1", "code": "stale_member"})
    task: Obj = {"source": "team_task", "text": "Topic: batteries.", "mail_id": f"{LEAD_BRANCH}:c1"}
    host(log, "user_input", task)
    out.append(
        (
            "team-task-taken-twice-rejected",
            "Rule 31: a refused task mail is taken as the task.",
            log,
        )
    )

    log = lead_log()
    root = text(user(log, "Run two.")["event_id"])
    notice = _note(
        f"{MEMBER_BRANCH}:c1",
        eid(9, LEAD_BRANCH),
        "member_settled",
        monitor_id=f"{LEAD_BRANCH}:{eid(9, LEAD_BRANCH)}:task",
        result=completed(RESEARCHER, "Done."),
    )
    received(log, notice)
    out.append(
        (
            "team-midturn-notice-other-request-rejected",
            "Rule 34: a task monitor's notice of another request is consumed mid-turn.",
            log,
        )
    )

    log = lead_log()
    root = text(user(log, "Run one.")["event_id"])
    elsewhere = provenance(root, thread=LOG_THREAD)  # the same event_id in another thread
    route = Route(RESEARCHER, "lead", elsewhere)
    received(log, envelope(f"{MEMBER_BRANCH}:c1", "message", route, at(root), body=body("Hi.")))
    out.append(
        (
            "team-receipt-root-thread-differs-rejected",
            "Rule 34: mid-turn mail whose root request has the turn's event_id in another thread.",
            log,
        )
    )
    return out


def _ended_with_child() -> Log:
    """The lead starts a background child, then its turn fails and it ends; the child finishes
    after the end."""
    log = lead_log(("spawn_agent", *TEAM_TOOLS))
    user(log, "Scan it.")
    spawn: Obj = {"agent": "scanner", "prompt": "Do your part.", "background": True}
    call(log, "spawn_agent", spawn, "call_1")
    spawned: Obj = {
        "call_id": "call_1",
        "child_thread_id": KID,
        "agent_name": "scanner",
        "mode": "background",
        "isolation": "none",
    }
    log.add("agent_spawned", spawned)
    result(log, "call_1", "scanner started in the background", origin="deferred")
    log.add("turn_completed", {"reason": "error"})
    log.add("member_ended", {"result": failed(LEAD, "model_error")})
    finished: Obj = {"child_thread_id": KID, "status": "completed", "usage": tokens(40, 8)}
    log.add("agent_finished", finished)
    late: Obj = {
        "call_id": "call_1",
        "is_error": False,
        "completeness": "complete",
        "preview": "Clean.",
    }
    log.add("tool_result_late", late)
    return log


def _member_ended() -> Log:
    log = member_log(eid(4, LEAD_BRANCH))
    task: Obj = {"source": "team_task", "text": "Topic: batteries.", "mail_id": f"{LEAD_BRANCH}:c1"}
    host(log, "user_input", task)
    log.add("turn_completed", {"reason": "error"})
    log.add("member_ended", {"result": failed(RESEARCHER, "model_error")})
    return log


def _ended_cases() -> list[tuple[str, str, Log]]:
    out: list[tuple[str, str, Log]] = []

    log = _member_ended()
    log.add("user_input", {"source": "api", "text": "Again."}, actor="user", principal=ALICE)
    out.append(
        ("member-ended-then-user-input-rejected", "Rule 37: an ended member takes input.", log)
    )

    log = _ended_with_child()
    cause = log.events[-1]["event_id"]
    log.add("woken", {"causes": [cause]}, actor="host", principal=ALICE)
    out.append(
        (
            "member-ended-then-woken-rejected",
            "Rule 37: an ended lead is woken by its background child's late result. "
            "legacy-late-result-after-lead-ended-no-wake is the same log without the woken.",
            log,
        )
    )

    log = lead_log()
    root = text(user(log, "Wait for the researcher.")["event_id"])
    wait = log.add(
        "wait_started",
        {
            "wait_id": f"{LEAD_BRANCH}:c1",
            "members": [RESEARCHER],
            "mode": "all",
            "deadline": DEADLINE,
        },
    )
    monitor = f"{LEAD_BRANCH}:{wait['event_id']}:researcher-1"
    done = completed(RESEARCHER, "Done.")
    notice = _note(f"{MEMBER_BRANCH}:c1", root, "member_settled", monitor_id=monitor, result=done)
    received(log, notice)
    source: Obj = {"thread_id": MEMBER_THREAD, "branch_id": MEMBER_BRANCH, "seq": 5}
    log.add("member_observed", {"monitor_id": monitor, "result": done, "source": source})
    out.append(
        (
            "member-observed-after-fired-rejected",
            "Rule 39: member_observed names a settle monitor whose notification was received.",
            log,
        )
    )

    log = lead_log()
    user(log, "Start a researcher.")
    call(log, "start", {"agent": "researcher", "task": "Batteries."}, "c1")
    result(log, "c1", "refused")
    decision: Obj = {"op": "start", "decision": "deny", "source": "default", "target": "researcher"}
    log.add("message_policy_decided", {**decision, "call_id": "c1"})
    out.append(
        (
            "policy-decision-after-result-rejected",
            "Rule 42: message_policy_decided names a call whose result is recorded.",
            log,
        )
    )
    return out


def _operator_cases() -> list[tuple[str, str, Log]]:
    out: list[tuple[str, str, Log]] = []

    log = team_log()
    parent: Obj = {
        "thread_id": LEAD_THREAD,
        "branch_id": LEAD_BRANCH,
        "event_id": eid(1, LEAD_BRANCH),
        "relation": "team_member",
    }
    started: Obj = {
        "member": RESEARCHER,
        "agent": "researcher",
        "config_hash": config_hash("researcher"),
        "thread_id": "0192a000-0000-7000-8000-0000000000b2",
        "parent": parent,
        "provenance": provenance(eid(9, LOG_BRANCH), thread=LOG_THREAD),
    }
    log.add("member_started", started)
    out.append(
        (
            "operator-start-without-request-rejected",
            "Rule 42: a team log's member_started names no operator_request as its root request.",
            log,
        )
    )

    log = team_log()
    wait: Obj = {
        "wait_id": f"{LOG_BRANCH}:{REQUEST}",
        "members": [RESEARCHER],
        "mode": "all",
        "deadline": DEADLINE,
    }
    log.add("wait_started", wait)
    out.append(
        (
            "operator-wait-without-request-rejected",
            "Rule 42: a team log's wait_started names no operator_request in its wait_id.",
            log,
        )
    )
    return out


def _mail_lines() -> Log:
    """Five turns, each opened by one received mail, and a silent park notice in the last."""
    log = lead_log()
    op = provenance(eid(2, LOG_BRANCH), thread=LOG_THREAD)
    note = envelope(
        f"{LOG_BRANCH}:{REQUEST}",
        "message",
        Route({"operator": REQUEST}, "lead", op),
        at(eid(2, LOG_BRANCH), LOG_THREAD),
        body=body('<b>"Tom\'s" & co</b>'),
    )
    received(log, note)
    answer(log, "Noted.")
    root = eid(1, LEAD_BRANCH)
    ref = log.art(b"Which topic: <cells> & packs?", "text/plain")
    ask_id = f"{MEMBER_BRANCH}:c2"
    received(log, _note(ask_id, root, "ask", ask_id=ask_id, deadline=DEADLINE, body={"ref": ref}))
    answer(log, "Asked.")
    received(log, _note(f"{MEMBER_BRANCH}:c3", root, "bounce", code="stale_member"))
    answer(log, "Bounced.")
    output = log.art(b"Batteries: done.", "text/plain")
    done: Obj = {"member": RESEARCHER, "status": "completed", "output": {"ref": output}}
    task = f"{LEAD_BRANCH}:{eid(5, LEAD_BRANCH)}:task"
    received(
        log, _note(f"{MEMBER_BRANCH}:c4", root, "member_settled", monitor_id=task, result=done)
    )
    answer(log, "Settled.")
    end = f"{LEAD_BRANCH}:{eid(6, LEAD_BRANCH)}:researcher-1"
    ended = failed(RESEARCHER, "model_error")
    received(log, _note(f"{MEMBER_BRANCH}:c5", root, "member_ended", monitor_id=end, result=ended))
    parked = _note(
        f"{MEMBER_BRANCH}:c6", root, "member_parked", monitor_id=task, reason="awaiting_approval"
    )
    received(log, parked)
    return log
