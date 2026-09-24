# pyright: strict
"""Team wire vector cases for the lifecycle, wait, wake and input events."""

from __future__ import annotations

from .team_pieces import (
    DEADLINE,
    LEAD,
    LEAD_BRANCH,
    LEAD_THREAD,
    LOG_BRANCH,
    LOG_THREAD,
    MEMBER_THREAD,
    RESEARCHER,
    TEAM,
    completed,
    failed,
    ref,
)
from .team_wire_lines import (
    BIG,
    BUDGET,
    E1,
    HOST,
    MONITOR,
    PARENT,
    STARTED,
    line,
)

# (name, line, valid)
EVENT_CASES: tuple[tuple[str, str, bool], ...] = (
    (
        "team_opened",
        line("team_opened", {"team": TEAM, "lead": LEAD, "lead_thread_id": LEAD_THREAD}),
        True,
    ),
    (
        "thread_started with team and a team_member parent",
        line(
            "thread_started",
            {
                "agent_name": "lead",
                "config_hash": "b" * 64,
                "model": {"provider": "scripted", "name": "s"},
                "model_params": {},
                "adapter": {"name": "scripted", "version": "1", "settings": {}},
                "instructions": "",
                "tools": [],
                "parent": PARENT,
                "team": {"id": TEAM, "log_thread_id": LOG_THREAD, "log_branch_id": LOG_BRANCH},
            },
        ),
        True,
    ),
    ("member_started", line("member_started", STARTED), True),
    (
        "member_started with a budget",
        line("member_started", {**STARTED, "budget": {"max_turns": 4}}),
        True,
    ),
    (
        "member_started whose parent is a subagent edge",
        line("member_started", {**STARTED, "parent": {**PARENT, "relation": "subagent"}}),
        False,
    ),
    ("member name not <agent>-<k>", line("monitor_set", {"member": ref("Researcher-1")}), False),
    ("member number 0", line("monitor_set", {"member": ref("researcher-0")}), False),
    ("generation 0", line("monitor_set", {"member": ref("researcher-1", 0)}), False),
    ("monitor_set", line("monitor_set", {"member": RESEARCHER}), True),
    (
        "member_idle, inline output",
        line("member_idle", {"result": completed(RESEARCHER, "Done.")}),
        True,
    ),
    (
        "member_idle, output as an artifact ref",
        line("member_idle", {"result": {**completed(RESEARCHER, ""), "output": {"ref": BIG}}}),
        True,
    ),
    (
        "member_idle output with text and ref",
        line(
            "member_idle",
            {"result": {**completed(RESEARCHER, ""), "output": {"text": "x", "ref": BIG}}},
        ),
        False,
    ),
    (
        "member_idle with a failed result",
        line("member_idle", {"result": failed(RESEARCHER, "pin_unavailable")}),
        False,
    ),
    (
        "member_ended failed pin_unavailable",
        line("member_ended", {"result": failed(RESEARCHER, "pin_unavailable")}),
        True,
    ),
    (
        "member_ended failed with an unknown code",
        line("member_ended", {"result": failed(RESEARCHER, "exploded")}),
        False,
    ),
    (
        "member_ended cancelled",
        line("member_ended", {"result": {"member": RESEARCHER, "status": "cancelled"}}),
        True,
    ),
    (
        "member_ended budget_exhausted",
        line(
            "member_ended",
            {"result": {"member": RESEARCHER, "status": "budget_exhausted", "budget": BUDGET}},
        ),
        True,
    ),
    (
        "member_ended completed",
        line("member_ended", {"result": completed(RESEARCHER, "Done.")}),
        False,
    ),
    (
        "member_observed",
        line(
            "member_observed",
            {
                "monitor_id": MONITOR,
                "result": completed(RESEARCHER, "Done."),
                "source": {"thread_id": MEMBER_THREAD, "branch_id": LEAD_BRANCH, "seq": 9},
            },
        ),
        True,
    ),
    (
        "wait_started all",
        line(
            "wait_started",
            {
                "wait_id": f"{LEAD_BRANCH}:c1",
                "members": [RESEARCHER],
                "mode": "all",
                "deadline": DEADLINE,
            },
        ),
        True,
    ),
    (
        "wait_started n of them",
        line(
            "wait_started",
            {
                "wait_id": f"{LEAD_BRANCH}:c1",
                "members": [RESEARCHER],
                "mode": 1,
                "deadline": DEADLINE,
            },
        ),
        True,
    ),
    (
        "wait_started with no members",
        line(
            "wait_started",
            {"wait_id": f"{LEAD_BRANCH}:c1", "members": [], "mode": "all", "deadline": DEADLINE},
        ),
        False,
    ),
    (
        "wait_started mode some",
        line(
            "wait_started",
            {
                "wait_id": f"{LEAD_BRANCH}:c1",
                "members": [RESEARCHER],
                "mode": "some",
                "deadline": DEADLINE,
            },
        ),
        False,
    ),
    (
        "wait_finished",
        line(
            "wait_finished",
            {
                "wait_id": f"{LEAD_BRANCH}:c1",
                "finished": [completed(RESEARCHER, "Done.")],
                "parked": [],
                "pending": [],
                "timed_out": False,
            },
        ),
        True,
    ),
    (
        "wait_finished with a parked member",
        line(
            "wait_finished",
            {
                "wait_id": f"{LEAD_BRANCH}:c1",
                "finished": [],
                "parked": [{"member": RESEARCHER, "reason": "awaiting_approval"}],
                "pending": [],
                "timed_out": True,
            },
        ),
        True,
    ),
    ("woken", line("woken", {"causes": [E1]}, HOST), True),
    ("woken without a principal", line("woken", {"causes": [E1]}), False),
    ("woken with no causes", line("woken", {"causes": []}, HOST), False),
    (
        "user_input team_task",
        line(
            "user_input",
            {"source": "team_task", "text": "Topic: batteries.", "mail_id": f"{LEAD_BRANCH}:c1"},
            HOST,
        ),
        True,
    ),
    (
        "user_input team_task without its mail_id",
        line("user_input", {"source": "team_task", "text": "Topic: batteries."}, HOST),
        False,
    ),
    (
        "user_input api with a mail_id",
        line("user_input", {"source": "api", "text": "hi", "mail_id": "x:c1"}, HOST),
        False,
    ),
    (
        "parked on an ask",
        line(
            "parked",
            {"address": {"kind": "ask", "id": f"{LEAD_BRANCH}:c1"}, "reason": "awaiting_member"},
        ),
        True,
    ),
    (
        "turn_completed error pin_mismatch",
        line("turn_completed", {"reason": "error", "code": "pin_mismatch"}),
        True,
    ),
    (
        "turn_completed end_turn with pin_unavailable",
        line("turn_completed", {"reason": "end_turn", "code": "pin_unavailable"}),
        False,
    ),
)
