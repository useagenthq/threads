# pyright: strict
"""Agents as line 0 names them, and team tools: the pinned sentences and specs."""

from __future__ import annotations

import json
import pathlib
from typing import TYPE_CHECKING

from .log import Log
from .pieces import render_case, started, user
from .policies import policy

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

FAM = "agents_teams"

CATALOG = pathlib.Path(__file__).resolve().parents[2] / "schema" / "tools.v1.catalog.json"
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


def _catalog_specs(names: tuple[str, ...]) -> list[JsonValue]:
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
    started(log, _catalog_specs(LEAD_TOOLS), instructions, policy(handoffs=["billing"]))
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
