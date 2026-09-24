# pyright: strict
"""The agent definitions of the agent pin vector (agent_pins.py): one per feature an agent can
pin without an app schema. The flag pins the agent as a team member, and a dynamic agent's
member with its starter's choice."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .jcs import Obj


def cases() -> list[tuple[str, Obj, bool, Obj | None]]:
    brief = {"instructions": "Be brief."}
    researcher: Obj = {"name": "researcher", "instructions": "Research."}
    p1: Obj = {"input": 3000, "output": 15000}
    analyst: Obj = {
        "name": "analyst",
        "instructions": "Analyse.",
        "sandbox": "fake",
        "models": [
            {"key": "fast", "name": "scripted-1"},
            {"key": "deep", "name": "scripted-big", "price": p1},
        ],
    }
    return [
        ("bare-agent", {"name": "bare", **brief}, False, None),
        (
            "partial-settings",
            {
                "name": "partial",
                **brief,
                "permissions": {"mode": "accept_edits"},
                "retry": {"max_retries": 3},
                "context": {"reserve_tokens": 10_000},
            },
            False,
            None,
        ),
        (
            "subagents",
            {
                "name": "lead",
                "instructions": "Delegate.",
                "subagents": [
                    {"name": "reviewer", "instructions": "Review.", "retry": {"max_retries": 1}}
                ],
            },
            False,
            None,
        ),
        ("team-lead", {"name": "lead", "instructions": "Lead.", "team": [researcher]}, False, None),
        ("team-member", researcher, True, None),
        (
            "extensions",
            {
                "name": "audited",
                **brief,
                "extensions": [
                    {
                        "name": "audit",
                        "instructions": "Audit every answer.",
                        "hooks": ["notification", "session_end"],
                        "observers": ["tool_result", "model_response"],
                    },
                    {"name": "timed", "hook_timeout_ms": 7000},
                ],
            },
            False,
            None,
        ),
        ("sandbox", {"name": "coder", **brief, "sandbox": "fake"}, False, None),
        (
            "priced-fallback-and-budget",
            {
                "name": "priced",
                **brief,
                "model": {"name": "scripted-1", "price": p1},
                "fallback": [{"name": "scripted-small", "price": {"input": 1000, "output": 5000}}],
                "budget": {"max_cost_nanos": 1_000_000_000},
                "on_unknown_usage": "stop",
            },
            False,
            None,
        ),
        (
            "agreed-cache-ttl",
            {
                "name": "cached",
                **brief,
                "model": {"name": "scripted-1", "cache_ttl_ms": 3_600_000},
                "fallback": [{"name": "scripted-small", "cache_ttl_ms": 3_600_000}],
            },
            False,
            None,
        ),
        (
            "explicit-cache-ttl",
            {
                "name": "cached",
                **brief,
                "model": {"name": "scripted-1", "cache_ttl_ms": 3_600_000},
                "fallback": [{"name": "scripted-small", "cache_ttl_ms": 300_000}],
                "context": {"cache_ttl_ms": 600_000},
            },
            False,
            None,
        ),
        (
            "handoffs-and-output-styles",
            {
                "name": "front",
                **brief,
                "handoffs": [{"name": "billing", "instructions": "Bill."}],
                "output_styles": {"terse": "Be terse.", "warm": "Be warm."},
            },
            False,
            None,
        ),
        ("memory", {"name": "remembers", **brief, "memory_write": "allow"}, False, None),
        (
            "skills",
            {
                "name": "skilled",
                **brief,
                "skills": [
                    {"name": "deploy", "description": "Ship it.", "body": "Run the deploy."}
                ],
            },
            False,
            None,
        ),
        (
            "dynamic-template-lead",
            {"name": "lead", "instructions": "Lead.", "team": [analyst]},
            False,
            None,
        ),
        (
            "dynamic-member",
            analyst,
            True,
            {
                "define": {
                    "instructions": "Find the flaky test.",
                    "tools": ["grep", "read"],
                    "model": "deep",
                },
                "starter": "lead",
            },
        ),
        (
            "dynamic-member-defaults",
            analyst,
            True,
            {"define": {"tools": ["bash"], "model": "fast"}, "starter": "operator"},
        ),
    ]
