# pyright: strict
"""The agent pin vector (spec/conformance/vectors/agent-pins.json; spec/schema/README.md, "The
pinned config"): agent definitions and the thread_started and config_hash each pins. The expected
pin comes from the rule below, never from an implementation: permissions, retry and context are
always pinned complete, the defaults filled in under the fields the agent sets. Both runtimes
build each agent with the scripted model and must pin these bytes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ADAPTER, CASES, MODEL, PARAMS, sha
from .jcs import canonical
from .pieces import dump
from .policies import CONTEXT, RETRY, permissions
from .teams import catalog_specs

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

VECTOR = CASES.parent / "vectors" / "agent-pins.json"
LIMITS: Obj = {
    "provider": "scripted",
    "name": "scripted-1",
    "context_window": 200_000,
    "max_output_tokens": 8192,
    "input_billing_bound": "context_window",
}
# The ADR defaults: the shared cases' sections, less the two values they shrink for their logs.
DEFAULTS: dict[str, Obj] = {
    "permissions": permissions("default"),
    "retry": {**RETRY, "fallback_after": 3},
    "context": {
        **CONTEXT,
        "compact": {
            "trigger": {"permille": 850},
            "keep_tail": {"tokens": 20_000},
            "max_failures": 3,
        },
    },
}
SUBAGENT_TOOLS = (
    "read_tool_result",
    "send_message",
    "spawn_agent",
    "team_task_claim",
    "team_task_create",
    "team_task_update",
    "todo_write",
)


def _started(agent: Obj, instructions: str, tools: tuple[str, ...]) -> Obj:
    """thread_started's data, as agent() pins it with the scripted model."""
    policy: Obj = {"models": [LIMITS]}
    for k, base in DEFAULTS.items():
        given = agent.get(k, {})
        policy[k] = {**base, **(given if isinstance(given, dict) else {})}
    cfg: Obj = {
        "agent_name": agent["name"],
        "instructions": instructions,
        "model": MODEL,
        "model_params": PARAMS,
        "adapter": ADAPTER,
        "tools": catalog_specs(tools),
        "policy": policy,
    }
    # No extensions, skills, memory, sandbox or concurrent tools: the config is the pin itself.
    return {**cfg, "config_hash": sha(canonical(cfg))}


def _cases() -> list[tuple[str, Obj, Obj]]:
    bare: Obj = {"name": "bare", "instructions": "Be brief."}
    partial: Obj = {
        "name": "partial",
        "instructions": "Be brief.",
        "permissions": {"mode": "accept_edits"},
        "retry": {"max_retries": 3},
        "context": {"reserve_tokens": 10_000},
    }
    reviewer: Obj = {"name": "reviewer", "instructions": "Review.", "retry": {"max_retries": 1}}
    subagents: Obj = {"name": "lead", "instructions": "Delegate.", "subagents": [reviewer]}
    researcher: Obj = {"name": "researcher", "instructions": "Research."}
    lead: Obj = {"name": "lead", "instructions": "Lead.", "team": [researcher]}
    basic = ("read_tool_result", "todo_write")
    return [
        ("bare-agent", bare, _started(bare, "Be brief.", basic)),
        ("partial-settings", partial, _started(partial, "Be brief.", basic)),
        (
            "subagents",
            subagents,
            _started(
                subagents,
                "Delegate.\n\nSubagents you can start with spawn_agent: reviewer.",
                SUBAGENT_TOOLS,
            ),
        ),
        (
            "team-lead",
            lead,
            _started(
                lead,
                "Lead.\n\nAgents you can start as team members with start: researcher.",
                ("read_tool_result", "send", "start", "todo_write"),
            ),
        ),
    ]


def _vector() -> str:
    cases: list[JsonValue] = [
        {"name": name, "agent": agent, "thread_started": started}
        for name, agent, started in _cases()
    ]
    doc: Obj = {
        "description": (
            "Agent pins (spec/schema/README.md, The pinned config): build `agent` with the "
            "scripted model, each given permissions, retry or context section being the default "
            "one with those fields replaced. A new thread's thread_started data "
            "must equal `thread_started`, config_hash included; a team lead's `team` ids are "
            "fresh per thread and left out."
        ),
        "cases": cases,
    }
    return dump(doc)


def write() -> None:
    VECTOR.write_text(_vector(), encoding="utf-8")


def check() -> list[str]:
    fresh = _vector()
    current = VECTOR.read_text(encoding="utf-8") if VECTOR.exists() else ""
    return [] if fresh == current else [f"{VECTOR.name}: differs; run gen_fixtures.py"]
