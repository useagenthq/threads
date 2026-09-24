# pyright: strict
"""The agent pin vector (spec/conformance/vectors/agent-pins.json; spec/schema/README.md, "The
pinned config"): agent definitions and the thread_started and config_hash each pins. The expected
pin comes from the rules below, never from an implementation: permissions, retry and context are
always pinned complete, the defaults filled in under the fields the agent sets; an extension pins
its hooks in wire-enum order and a 5000 ms timeout by default; a sandbox pins its provider,
egress, capture classes and the egress policy, deny-all ([]) by default. Both runtimes build each
agent with scripted models and must pin these bytes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .agent_pin_cases import cases
from .common import ADAPTER, CASES, PARAMS, arr, num, obj, sha, text
from .jcs import JsonValue, canonical
from .pieces import dump
from .policies import CONTEXT, RETRY, permissions
from .teams import catalog_specs

if TYPE_CHECKING:
    from .jcs import Obj

VECTOR = CASES.parent / "vectors" / "agent-pins.json"
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
DEFAULT_TTL_MS = 300_000
HOOK_TIMEOUT_MS = 5000
HOOKS = (  # the wire enum's order
    "session_start",
    "session_end",
    "before_input",
    "before_model",
    "after_model",
    "before_tool",
    "permission_request",
    "permission_denied",
    "after_tool",
    "before_tool_result",
    "after_tool_batch",
    "before_compact",
    "after_compact",
    "on_stop",
    "stop_failure",
    "subagent_start",
    "subagent_stop",
    "before_model_switch",
    "after_model_switch",
    "notification",
)
SANDBOX_TOOLS = ("bash", "edit", "glob", "grep", "ls", "notebook_edit", "read", "write")
TASK_BOARD = ("send_message", "team_task_claim", "team_task_create", "team_task_update")
EFFECTS: Obj = {
    "bash": {"effect_class": "sandbox_local"},
    "edit": {"effect_class": "sandbox_local"},
    "notebook_edit": {"effect_class": "sandbox_local"},
    "write": {"effect_class": "sandbox_local"},
    "save_memory": {"effect_class": "idempotent", "dedup_window_ms": 2**53 - 1},
    "forget_memory": {"effect_class": "idempotent", "dedup_window_ms": 2**53 - 1},
}
FAKE_SANDBOX: Obj = {"provider": "fake", "egress": "enforced", "capture_classes": ["filesystem"]}


def _names(agent: Obj, key: str) -> list[str]:
    return [text(obj(a)["name"]) for a in arr(agent.get(key, []))]


def _tools(agent: Obj, member: bool) -> list[JsonValue]:
    """Built-ins and framework tools, catalog entries sorted by name."""
    names = ["read_tool_result", "todo_write"]
    names += list(SANDBOX_TOOLS) if "sandbox" in agent else []
    names += ["spawn_agent", *TASK_BOARD] if "subagents" in agent else []
    names += ["handoff"] if "handoffs" in agent else []
    names += ["send", "start"] if "team" in agent or member else []
    names += ["search_memory", "save_memory", "forget_memory"] if "memory_write" in agent else []
    names += ["load_skill"] if "skills" in agent else []
    specs = [obj(s) for s in catalog_specs(tuple(names))]
    return [{**s, **obj(EFFECTS.get(text(s["name"]), {}))} for s in specs]


def _instructions(agent: Obj) -> str:
    skills = [obj(s) for s in arr(agent.get("skills", []))]
    parts = [text(agent["instructions"])]
    parts += [text(obj(e).get("instructions", "")) for e in arr(agent.get("extensions", []))]
    if skills:
        lines = [f"- {text(s['name'])}: {text(s['description'])}" for s in skills]
        parts.append("\n".join(["Skills you can load with load_skill:", *lines]))
    for key, label in (
        ("subagents", "Subagents you can start with spawn_agent"),
        ("handoffs", "Agents you can hand the conversation to"),
        ("team", "Agents you can start as team members with start"),
    ):
        if key in agent:
            parts.append(f"{label}: {', '.join(_names(agent, key))}.")
    return "\n\n".join(p for p in parts if p)


def _model(m: Obj) -> Obj:
    """A scripted model under another name, declaring a price and a cache lifetime when given."""
    limits: Obj = {
        "provider": "scripted",
        "name": m["name"],
        "context_window": 200_000,
        "max_output_tokens": 8192,
        "input_billing_bound": "context_window",
    }
    return {**limits, **({"price": m["price"]} if "price" in m else {})}


def _ttl(models: list[Obj], context: Obj) -> JsonValue:
    """The agent's cache_ttl_ms, else the models' agreed lifetime ("none" never caches)."""
    if "cache_ttl_ms" in context:
        return context["cache_ttl_ms"]
    ttls = {num(m["cache_ttl_ms"]) for m in models if "cache_ttl_ms" in m}
    if len(ttls) > 1:
        raise AssertionError("disagreeing cache lifetimes need context.cache_ttl_ms")
    return ttls.pop() if ttls else DEFAULT_TTL_MS


def _settings(m: Obj) -> Obj:
    return {
        "model": {"provider": "scripted", "name": m["name"]},
        "model_params": PARAMS,
        "adapter": ADAPTER,
    }


def _policy(agent: Obj) -> Obj:
    models = [obj(agent.get("model", {"name": "scripted-1"}))] + [
        obj(f) for f in arr(agent.get("fallback", []))
    ]
    limits = [_model(m) for m in models]
    policy: Obj = {"models": [m for i, m in enumerate(limits) if m not in limits[:i]]}
    if any("price" in m for m in limits):
        policy["currency"] = "USD"
    for k, base in DEFAULTS.items():
        policy[k] = {**base, **obj(agent.get(k, {}))}
    obj(policy["context"])["cache_ttl_ms"] = _ttl(models, obj(agent.get("context", {})))
    if len(models) > 1:
        policy["fallback"] = [{**_settings(m), "reasoning_carryover": "keep"} for m in models[1:]]
    for key in ("budget", "on_unknown_usage", "output_styles"):
        if key in agent:
            policy[key] = agent[key]
    if "handoffs" in agent:
        policy["handoffs"] = list[JsonValue](_names(agent, "handoffs"))
    return policy


def _hashed(agent: Obj) -> Obj:
    """Pinned by config_hash but not in thread_started."""
    out: Obj = {}
    if "extensions" in agent:
        out["extensions"] = [
            {
                "name": e["name"],
                "hooks": list[JsonValue](h for h in HOOKS if h in arr(e.get("hooks", []))),
                "observers": list[JsonValue](sorted(text(o) for o in arr(e.get("observers", [])))),
                "hook_timeout_ms": e.get("hook_timeout_ms", HOOK_TIMEOUT_MS),
            }
            for e in map(obj, arr(agent["extensions"]))
        ]
    if "skills" in agent:
        out["skills"] = [
            {
                "name": s["name"],
                "description": s["description"],
                "sha256": sha(text(s["body"]).encode()),
            }
            for s in map(obj, arr(agent["skills"]))
        ]
    if "memory_write" in agent:
        out["memory_write"] = agent["memory_write"]
    if "sandbox" in agent:
        out["sandbox"] = {**FAKE_SANDBOX, "policy": []}
    return out


def _pin(agent: Obj, member: bool = False) -> Obj:
    """thread_started's data, as agent() pins it (as a team member's with `member`)."""
    first = obj(agent.get("model", {"name": "scripted-1"}))
    data: Obj = {
        "agent_name": agent["name"],
        "instructions": _instructions(agent),
        **_settings(first),
        "tools": _tools(agent, member),
        "policy": _policy(agent),
        **({"sandbox_provider": "fake"} if "sandbox" in agent else {}),
    }
    return {**data, "config_hash": sha(canonical({**data, **_hashed(agent)}))}


def _vector() -> str:
    rows: list[JsonValue] = [
        {"name": name, "agent": agent, "team_member": member, "thread_started": _pin(agent, member)}
        for name, agent, member in cases()
    ]
    doc: Obj = {
        "description": (
            "Agent pins (spec/schema/README.md, The pinned config). Build `agent`: models are "
            "scripted models under `name`, declaring `price` and a `cache_ttl_ms` lifetime when "
            "given ('none' otherwise; `model` absent: scripted-1); each given permissions, retry "
            "or context section is the default one with those fields replaced; `sandbox` is the "
            "fake sandbox; `memory_write` gives local memory; extension hooks and observers are "
            "no-ops. A new thread's thread_started data (a team member's pin, with `team_member`) "
            "must equal `thread_started`, config_hash included; a team lead's `team` ids are "
            "fresh per thread and left out."
        ),
        "cases": rows,
    }
    return dump(doc)


def write() -> None:
    VECTOR.write_text(_vector(), encoding="utf-8")


def check() -> list[str]:
    fresh = _vector()
    current = VECTOR.read_text(encoding="utf-8") if VECTOR.exists() else ""
    return [] if fresh == current else [f"{VECTOR.name}: differs; run gen_fixtures.py"]
