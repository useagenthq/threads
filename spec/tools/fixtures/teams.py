# pyright: strict
"""Agents as line 0 names them, and team tools: the pinned sentences and specs."""

from __future__ import annotations

import json
import pathlib
from typing import TYPE_CHECKING

from .common import ALLOW, NOW, tokens
from .log import Log, reduce
from .pieces import FINAL, TAIL, case, render_case, started, user, write_case
from .policies import policy

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

FAM = "agents_teams"

CATALOG = pathlib.Path(__file__).resolve().parents[2] / "schema" / "tools.v1.catalog.json"
TEAM_TOOLS = ("send_message", "team_task_claim", "team_task_create", "team_task_update")
LEAD_TOOLS = (
    "handoff",
    "read_tool_result",
    "send_message",
    "spawn_agent",
    "team_task_claim",
    "team_task_create",
    "team_task_update",
    "todo_write",
)


def catalog_specs(names: tuple[str, ...]) -> list[JsonValue]:
    """Pinned specs of catalog tools, as the runtimes pin them: read_only, sorted by name."""
    listed: list[Obj] = json.loads(CATALOG.read_bytes())
    return [{**e, "effect_class": "read_only"} for e in listed if e["name"] in names]


def _listed(root: pathlib.Path) -> None:
    log = Log()
    instructions = "\n\n".join(
        (
            "You coordinate a code review.",
            "Subagents you can start with spawn_agent: reviewer, scanner.",
            "Agents you can hand the conversation to: billing.",
        )
    )
    started(log, catalog_specs(LEAD_TOOLS), instructions, policy(handoffs=["billing"]))
    user(log, "Review the diff.")
    render_case(
        root,
        (
            "render-agents-listed-in-system",
            FAM,
            "An agent with subagents reviewer and scanner and handoff target billing: line 0's "
            "system ends with the two pinned agent sentences, in declaration order, joined by "
            "blank lines; its tools are the framework catalog entries, read_only, sorted by name "
            "with the built-ins.",
        ),
        log,
    )


def build(root: pathlib.Path) -> None:
    _listed(root)
    _replay(root)


def _replay(root: pathlib.Path) -> None:
    """The lead's team_task_create and send_message reached its log, then the process died
    before their results. Recovery dispatches both again: each answers from the log (its id is
    the member's call), appends only its tool_result, and duplicates nothing."""
    log = Log()
    started(log, catalog_specs(TEAM_TOOLS))
    user(log, "Set up the team.")
    r = log.model_request()
    create: Obj = {"subject": "Write the schema"}
    send: Obj = {"to": "*", "text": "Schema first."}
    uses: list[JsonValue] = [
        {"type": "tool_use", "call_id": "c1", "name": "team_task_create", "input": create},
        {"type": "tool_use", "call_id": "c2", "name": "send_message", "input": send},
    ]
    log.model_response(r, uses, "tool_use", tokens(80, 20))
    log.tool_call(r, "c1", "team_task_create", create)
    log.tool_call(r, "c2", "send_message", send)
    log.add("permission_decision", {"call_id": "c1", **ALLOW})
    log.add("permission_decision", {"call_id": "c2", **ALLOW})
    log.add(
        "team_task_created", {"task_id": "demo/c1", "subject": "Write the schema", "blocked_by": []}
    )
    log.add(
        "team_message",
        {"message_id": "demo/c2", "from": "demo", "to": "*", "text": "Schema first."},
    )

    def result(call_id: str, preview: str) -> Obj:
        return {
            "type": "tool_result",
            "actor_kind": "tool",
            "data": {
                "call_id": call_id,
                "is_error": False,
                "origin": "executed",
                "preview": preview,
            },
        }

    write_case(
        root,
        case(
            "team-tool-replay-appends-nothing",
            FAM,
            "recover",
            "A crash after the lead's team_task_created and team_message, before their results. "
            "Recovery dispatches both calls again: the task id and message id are the member's "
            "call (demo/c1, demo/c2), so each answers from the log and appends only its "
            "tool_result; no task or message is duplicated (F7.4).",
            NOW,
            model_script="model.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "appended": [result("c1", "demo/c1"), result("c2", "sent"), *TAIL],
        },
        extra={"model.json": {"responses": [FINAL]}},
    )
