# pyright: strict
"""Children, teams and todos as the parent's or lead's log records them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ADAPTER, ALICE, MODEL, PARAMS, sha, tokens
from .jcs import JsonValue, Obj, canonical
from .log import Log
from .pieces import answer, call, reduce_case, result, started, user
from .projections import children, team_tasks, todos
from .teams import TEAM_TOOLS, catalog_specs

if TYPE_CHECKING:
    import pathlib

FAM = "agents_teams"
PARENT_THREAD = "0192a000-0000-7000-8000-0000000000a1"
PARENT_BRANCH = "0192a000-0000-7000-8000-0000000000a2"
SPAWN_EVENT = "0192a000-0000-7000-8000-0000000000a3"
KIDS = [f"0192a000-0000-7000-8000-0000000000c{i}" for i in range(1, 5)]
SPAWN = catalog_specs(("spawn_agent",))
TODO = catalog_specs(("todo_write",))


def build(root: pathlib.Path) -> None:
    _child_thread(root)
    _background(root)
    _terminal(root)
    _child_budget(root)
    _cancel_tree(root)
    _child_parks(root)
    _team(root)
    _malformed_todos(root)


def _spawn(log: Log, cid: str, kid: str, mode: str = "foreground", agent: str = "worker") -> None:
    call(log, "spawn_agent", {"agent": agent, "prompt": "Do your part."}, cid)
    log.add(
        "agent_spawned",
        {
            "call_id": cid,
            "child_thread_id": kid,
            "agent_name": agent,
            "mode": mode,
            "isolation": "none",
        },
    )


def _finish(log: Log, kid: str, status: str, output: bytes) -> None:
    log.add(
        "agent_finished",
        {
            "child_thread_id": kid,
            "status": status,
            "output_ref": log.art(output, "text/plain"),
            "usage": tokens(40, 8),
        },
    )


def _child_thread(root: pathlib.Path) -> None:
    log = Log()
    cfg: Obj = {
        "agent_name": "reviewer",
        "instructions": "You review diffs.",
        "model": MODEL,
        "model_params": PARAMS,
        "adapter": ADAPTER,
        "tools": catalog_specs(("read_tool_result", *TEAM_TOOLS, "todo_write")),
        "policy": {"budget": {"max_model_requests": 5}},
    }
    parent: Obj = {
        "thread_id": PARENT_THREAD,
        "branch_id": PARENT_BRANCH,
        "event_id": SPAWN_EVENT,
        "relation": "subagent",
    }
    log.add("thread_started", {**cfg, "config_hash": sha(canonical(cfg)), "parent": parent})
    log.add(
        "user_input",
        {"source": "parent_agent", "text": "Review the diff."},
        actor="host",
        principal=ALICE,
    )
    answer(log, "LGTM with one nit.")
    reduce_case(
        root,
        (
            "subagent-child-thread",
            FAM,
            "A child's own log: thread_started links the parent's agent_spawned (relation "
            "subagent) and pins the child's narrowed tools and budget; its first user_input is "
            "the prompt from parent_agent under the originating principal. It reduces alone.",
        ),
        log,
        {},
    )


def _background(root: pathlib.Path) -> None:
    log = Log()
    started(log, SPAWN)
    user(log, "Scan the dependencies in the background.")
    _spawn(log, "call_1", KIDS[0], "background", "scanner")
    result(log, "call_1", "scanner started in the background", origin="deferred")
    answer(log, "The scan is running.")
    _finish(log, KIDS[0], "completed", b"No vulnerable dependencies.")
    log.add(
        "tool_result_late",
        {
            "call_id": "call_1",
            "is_error": False,
            "completeness": "complete",
            "preview": "No vulnerable dependencies.",
        },
    )
    reduce_case(
        root,
        (
            "subagent-background-notify",
            FAM,
            "A background child's call gets a deferred placeholder at once; after the turn "
            "ends its one agent_finished and tool_result_late arrive, and the late result is "
            "the next request's notification.",
        ),
        log,
        {"children": children(log)},
    )


def _terminal(root: pathlib.Path) -> None:
    log = Log()
    started(log, SPAWN)
    user(log, "Run all four.")
    outcomes = ("completed", "failed", "cancelled", "budget_exhausted")
    for i, (kid, status) in enumerate(zip(KIDS, outcomes, strict=True)):
        cid = f"call_{i + 1}"
        _spawn(log, cid, kid)
        _finish(log, kid, status, status.encode())
        preview = "done" if status == "completed" else f"{status}: {status}"
        result(log, cid, preview, is_error=status != "completed")
    answer(log, "One of four succeeded.")
    reduce_case(
        root,
        (
            "child-terminal-results",
            FAM,
            "Four children end completed, failed, cancelled and budget_exhausted: exactly one "
            "agent_finished each, before its call's result. The children projection lists "
            "each terminal status.",
        ),
        log,
        {"children": children(log)},
    )


def _child_budget(root: pathlib.Path) -> None:
    log = Log()
    started(log, SPAWN)
    user(log, "Have the worker do it.")
    _spawn(log, "call_1", KIDS[0])
    _finish(log, KIDS[0], "budget_exhausted", b"budget exhausted: max_model_requests")
    result(
        log,
        "call_1",
        "budget_exhausted: budget exhausted: max_model_requests",
        is_error=True,
    )
    answer(log, "The worker ran out of budget; here is what I have.")
    reduce_case(
        root,
        (
            "child-budget-exhausted",
            FAM,
            "A child that hit its budget ends budget_exhausted; its call gets an error result "
            "and the parent continues its turn to end_turn.",
        ),
        log,
        {"children": children(log)},
    )


def _cancel_tree(root: pathlib.Path) -> None:
    log = Log()
    started(log, SPAWN)
    user(log, "Have the worker do it.")
    _spawn(log, "call_1", KIDS[0])
    cr = log.add("cancel_requested", {"scope": "thread"}, actor="user", principal=ALICE)
    _finish(log, KIDS[0], "cancelled", b"cancelled")
    result(log, "call_1", "cancelled: cancelled", is_error=True)
    log.add("cancelled", {"request_event_id": cr["event_id"]})
    log.add("turn_completed", {"reason": "cancelled"})
    reduce_case(
        root,
        (
            "cancel-accepted-then-stopped",
            "cancellation_resume",
            "A parent cancelled while its foreground child runs: cancel_requested is the "
            "barrier; the child (sent cancel_requested{scope: tree}) ends cancelled and its one "
            "agent_finished and call result are recorded before the parent's cancelled and "
            "turn_completed{cancelled}. The thread reduces cancelled.",
        ),
        log,
        {"children": children(log)},
    )


def _child_parks(root: pathlib.Path) -> None:
    log = Log()
    started(log, SPAWN)
    user(log, "Have the worker send it.")
    _spawn(log, "call_1", KIDS[0])
    log.add(
        "parked",
        {"address": {"kind": "child", "id": KIDS[0]}, "reason": "awaiting_approval"},
    )
    reduce_case(
        root,
        (
            "child-parks-parent",
            FAM,
            "A foreground child that parks (here on an approval) records no agent_finished: "
            "the parent parks on {kind: child, id: <child_thread_id>} with the child's reason, "
            "its spawn call stays pending and the child is still running.",
        ),
        log,
        {"children": children(log)},
    )


def _team(root: pathlib.Path) -> None:
    log = Log()
    started(log, catalog_specs(TEAM_TOOLS))
    for task, subject in (("lead/c1", "Write the schema"), ("lead/c2", "Write the runner")):
        log.add("team_task_created", {"task_id": task, "subject": subject, "blocked_by": []})
    log.add("team_task_claimed", {"task_id": "lead/c1", "member": "alice"})
    log.add("team_task_claimed", {"task_id": "lead/c2", "member": "bob"})
    for mid, sender, to, text in (
        ("alice/a2", "alice", "bob", "Use snake_case field names."),
        ("bob/b2", "bob", "alice", "Will do."),
    ):
        log.add("team_message", {"message_id": mid, "from": sender, "to": to, "text": text})
    log.add("team_task_updated", {"task_id": "lead/c1", "status": "completed"})
    reduce_case(
        root,
        (
            "team-shared-tasks-messages",
            FAM,
            "Two members claim one task each from the lead's shared list and exchange "
            "addressed messages; every claim and message is an event in the lead's log.",
        ),
        log,
        {"team_tasks": team_tasks(log)},
    )


def _malformed_todos(root: pathlib.Path) -> None:
    log = Log()
    started(log, TODO)
    user(log, "Plan it.")
    items: list[JsonValue] = [{"id": "1", "content": "Add the column", "status": "pending"}]
    call(log, "todo_write", {"todos": items}, "call_1")
    log.add("todos_updated", {"call_id": "call_1", "todos": items})
    result(log, "call_1", "ok")
    bad: list[JsonValue] = [{"id": "1", "content": "Add the column", "status": "done"}]
    call(log, "todo_write", {"todos": bad}, "call_2")
    log.add(
        "tool_result",
        {
            "call_id": "call_2",
            "is_error": True,
            "completeness": "complete",
            "preview": "invalid input: status must be pending, in_progress or completed",
            "origin": "not_executed",
        },
        actor="host",
    )
    answer(log, "Planned.")
    reduce_case(
        root,
        (
            "todos-malformed-rejected",
            "tasks",
            "A todo_write whose list fails the tool's schema gets an error result before any "
            "effect and appends no todos_updated, so the prior list stands.",
        ),
        log,
        {"todos": todos(log)},
    )
