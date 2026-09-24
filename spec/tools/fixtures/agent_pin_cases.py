# pyright: strict
"""The agent definitions of the agent pin vector (agent_pins.py): one per feature an agent can
pin without an app schema. The flag pins the agent as a team member."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .jcs import Obj


def cases() -> list[tuple[str, Obj, bool]]:
    brief = {"instructions": "Be brief."}
    researcher: Obj = {"name": "researcher", "instructions": "Research."}
    p1: Obj = {"input": 3000, "output": 15000}
    return [
        ("bare-agent", {"name": "bare", **brief}, False),
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
        ),
        ("team-lead", {"name": "lead", "instructions": "Lead.", "team": [researcher]}, False),
        ("team-member", researcher, True),
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
        ),
        ("sandbox", {"name": "coder", **brief, "sandbox": "fake"}, False),
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
        ),
        ("memory", {"name": "remembers", **brief, "memory_write": "allow"}, False),
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
        ),
    ]
