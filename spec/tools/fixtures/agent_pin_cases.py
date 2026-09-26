# pyright: strict
"""The agent definitions of the agent pin vector (agent_pins.py): one per feature an agent can
pin without an app schema. A member name pins that agent of the lead's team as a member, a
dynamic one as the member its starter's choice defines."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj


def cases() -> list[tuple[str, Obj, str | None, Obj | None]]:
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
        ("bare-agent", {"name": "bare", **brief}, None, None),
        (
            "partial-settings",
            {
                "name": "partial",
                **brief,
                "permissions": {"mode": "accept_edits"},
                "retry": {"max_retries": 3},
                "context": {"reserve_tokens": 10_000},
            },
            None,
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
            None,
            None,
        ),
        ("team-lead", {"name": "lead", "instructions": "Lead.", "team": [researcher]}, None, None),
        (
            "team-member-inherits-defer-tools",
            {
                "name": "lead",
                "instructions": "Lead.",
                "team": [researcher],
                "context": {"defer_tools": "never"},
            },
            "researcher",
            None,
        ),
        (
            "defer-tools",
            {"name": "deferring", **brief, "context": {"defer_tools": "always"}},
            None,
            None,
        ),
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
            None,
            None,
        ),
        ("sandbox", {"name": "coder", **brief, "sandbox": "fake"}, None, None),
        (
            "sandbox-open-egress",
            {"name": "coder", **brief, "sandbox": "fake", "egress": "unenforced"},
            None,
            None,
        ),
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
            None,
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
            None,
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
            None,
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
            None,
            None,
        ),
        ("memory", {"name": "remembers", **brief, "memory_write": "allow"}, None, None),
        (
            "workspace-files",
            {
                "name": "coder",
                **brief,
                "sandbox": "fake",
                "workspace": {"files": {"NOTES.md": "# notes\n", "docs/plan.md": "Plan.\n"}},
            },
            None,
            None,
        ),
        (
            "workspace-local-dir",
            {
                "name": "coder",
                **brief,
                "sandbox": "fake",
                "workspace": {
                    "files": {"NOTES.md": "# notes\n"},
                    "local_dir": "./app",
                    "include": [".env.test"],
                },
            },
            None,
            None,
        ),
        (
            "skills",
            {
                "name": "skilled",
                **brief,
                "skills": [
                    {"name": "deploy", "description": "Ship it.", "body": "Run the deploy."}
                ],
            },
            None,
            None,
        ),
        (
            "dynamic-template-lead",
            {"name": "lead", "instructions": "Lead.", "team": [analyst]},
            None,
            None,
        ),
        (
            "dynamic-member",
            {
                "name": "lead",
                "instructions": "Lead.",
                "team": [analyst],
                "context": {"defer_tools": "always"},
            },
            "analyst",
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
            {"name": "lead", "instructions": "Lead.", "team": [analyst]},
            "analyst",
            {"define": {"tools": ["bash"], "model": "fast"}, "starter": "operator"},
        ),
    ]


def directories() -> dict[str, list[JsonValue]]:
    """A local_dir case's directory contents: {path, text[, exec]}, {path, symlink} or
    {path, dir: true}, written under the case's local_dir before the agent is pinned."""
    return {
        "workspace-local-dir": [
            {"path": "src/main.ts", "text": "console.log(1);\n"},
            {"path": "bin/run.sh", "text": "#!/bin/sh\necho hi\n", "exec": True},
            {"path": "main.ts", "symlink": "src/main.ts"},
            {"path": "logs", "dir": True},
            {"path": ".env", "text": "MODE=prod\n"},
            {"path": ".env.test", "text": "MODE=test\n"},
            {"path": "id_rsa", "text": "not a key\n"},
            {"path": "id_ed25519", "text": "not a key\n"},
            {"path": "id_utils/index.ts", "text": "export {};\n"},
            {"path": "id_token.ts", "text": "export {};\n"},
            {"path": ".npmrc", "text": "registry=x\n"},
            {"path": ".ssh/config", "text": "Host x\n"},
            {"path": ".docker/config.json", "text": "{}\n"},
            {"path": "certs/server.pem", "text": "pem\n"},
            {"path": "config.json", "text": "{}\n"},
        ]
    }
