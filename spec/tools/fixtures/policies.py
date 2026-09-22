# pyright: strict
"""Pinned runtime policy sections (thread_started.policy) shared by the cases."""

from __future__ import annotations

from .common import ADAPTER, MODEL, PARAMS, sha, tool
from .jcs import JsonValue, Obj, canonical

SMALL: Obj = {"provider": "scripted", "name": "scripted-small"}
PRICE: Obj = {"input": 3000, "output": 15000, "cache_read": 300, "cache_write": 3750}
MODELS: list[JsonValue] = [
    {
        "provider": "scripted",
        "name": "scripted-1",
        "context_window": 200_000,
        "max_output_tokens": 8192,
        "price": PRICE,
    },
    {
        "provider": "scripted",
        "name": "scripted-small",
        "context_window": 200_000,
        "max_output_tokens": 8192,
        "price": {"input": 1000, "output": 5000},
    },
]
SMALL_SETTINGS: Obj = {
    "model": SMALL,
    "model_params": PARAMS,
    "adapter": ADAPTER,
    "reasoning_carryover": "omit_prior",
}
PRIMARY_SETTINGS: Obj = {
    "model": MODEL,
    "model_params": PARAMS,
    "adapter": ADAPTER,
    "reasoning_carryover": "keep",
}
RETRY: Obj = {
    "max_retries": 8,
    "base_delay_ms": 1000,
    "max_delay_ms": 32_000,
    "max_retry_after_ms": 60_000,
    "max_total_wait_ms": 600_000,
    "crash_resends": 2,
    "fallback_after": 2,
    "fallback_scope": "turn",
    "heartbeat_ms": 15_000,
}
CONTEXT: Obj = {
    "reserve_tokens": 20_000,
    "cache_ttl_ms": 300_000,
    "clear_results": {"trigger": {"permille": 700}, "keep_recent": 5, "exclude_tools": []},
    "spill": {
        "threshold_bytes": 32_768,
        "head_bytes": 2048,
        "tail_bytes": 1024,
        "request_budget_bytes": 204_800,
    },
    "compact": {"trigger": {"permille": 850}, "keep_tail": {"tokens": 1}, "max_failures": 3},
    "restore": {
        "max_files": 5,
        "file_tokens": 5000,
        "skill_tokens": 5000,
        "skills_total_tokens": 25_000,
    },
    "max_output_continuations": 3,
    "defer_tools": "auto",
    "defer_threshold": {"permille": 100},
    "server_edits": "disabled",
}
PROTECTED: list[JsonValue] = [
    ".git/**",
    ".threads/**",
    ".claude/**",
    ".mcp.json",
    "**/.bashrc",
    "**/.zshrc",
    "**/.profile",
    "**/.gitconfig",
    "**/.ssh/**",
]
OUTPUT_SCHEMA: Obj = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fixed"],
    "properties": {"fixed": {"type": "boolean"}},
}
OUTPUT: Obj = {
    "schema": OUTPUT_SCHEMA,
    "schema_sha256": sha(canonical(OUTPUT_SCHEMA)),
    "mode": "tool",
    "max_retries": 2,
}
FINAL_OUTPUT: Obj = {
    **tool("final_output", "Return the final structured result.", {}, "read_only"),
    "input_schema": OUTPUT_SCHEMA,
    "ends_turn": True,
}


def permissions(mode: str, allow_bypass: bool = False) -> Obj:
    return {
        "mode": mode,
        "allow": [],
        "ask": [],
        "deny": [],
        "protected_paths": PROTECTED,
        "allow_bypass": allow_bypass,
        "plan_exit_mode": "default",
    }


def policy(**sections: JsonValue) -> Obj:
    return {"models": MODELS, "currency": "USD", **sections}
