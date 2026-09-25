# pyright: strict
"""The agent pin vector (spec/conformance/vectors/agent-pins.json; spec/schema/README.md, "The
pinned config"): agent definitions and the thread_started and config_hash each pins. The expected
pin comes from the rules below, never from an implementation: permissions, retry and context are
always pinned complete, the defaults filled in under the fields the agent sets; an extension pins
its hooks in wire-enum order and a 5000 ms timeout by default; a sandbox pins its provider,
egress, capture classes and the egress policy, deny-all ([]) by default. Under open egress
(`egress: "unenforced"`) every sandbox_local built-in pins unguarded: a change may reach outside
the sandbox, so an in-doubt call parks. Both runtimes build each agent with scripted models and
must pin these bytes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .agent_pin_cases import cases
from .common import ADAPTER, CASES, PARAMS, arr, num, obj, sha, text
from .dynamic_rules import KEPT, block
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
FAKE_SANDBOX: Obj = {
    "provider": "fake",
    "egress": "enforced",
    "capture_classes": ["filesystem"],
}


def _names(agent: Obj, key: str) -> list[str]:
    return [text(obj(a)["name"]) for a in arr(agent.get(key, []))]


def _tools(agent: Obj, member: bool) -> list[JsonValue]:
    """Built-ins and framework tools, catalog entries sorted by name."""
    names = ["read_tool_result", "todo_write"]
    names += list(SANDBOX_TOOLS) if "sandbox" in agent else []
    names += ["spawn_agent", *TASK_BOARD] if "subagents" in agent else []
    names += ["handoff"] if "handoffs" in agent else []
    names += (
        ["ask", "monitor", "reply", "send", "start", "wait"] if "team" in agent or member else []
    )
    names += ["search_memory", "save_memory", "forget_memory"] if "memory_write" in agent else []
    names += ["load_skill"] if "skills" in agent else []
    specs = [obj(s) for s in catalog_specs(tuple(names))]
    pinned = [{**s, **obj(EFFECTS.get(text(s["name"]), {}))} for s in specs]
    if agent.get("egress") == "unenforced":  # a sandbox_local change may reach outside
        pinned = [
            {**s, "effect_class": "unguarded"} if s["effect_class"] == "sandbox_local" else s
            for s in pinned
        ]
    return list[JsonValue](pinned)


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
    ):
        if key in agent:
            parts.append(f"{label}: {', '.join(_names(agent, key))}.")
    if "team" in agent:
        names = ", ".join(_names(agent, "team"))
        lines = [_listed(obj(a)) for a in arr(agent["team"]) if "models" in obj(a)]
        parts.append(
            "\n".join([f"Agents you can start as team members with start: {names}.", *lines])
        )
    return "\n\n".join(p for p in parts if p)


def _choosable(template: Obj) -> list[str]:
    """A template's tools as a member, in pinned order, less the framework set F."""
    return [n for n in (text(obj(t)["name"]) for t in _tools(template, True)) if n not in KEPT]


def _listed(template: Obj) -> str:
    """A dynamic agent's line in its lead's team listing."""
    tools = ", ".join(_choosable(template)) or "none"
    keys = [text(obj(m)["key"]) for m in arr(template["models"])]
    models = ", ".join([f"{keys[0]} (default)", *keys[1:]])
    return (
        f"{text(template['name'])} (you write its instructions; tools: {tools}; models: {models})"
    )


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
        out["sandbox"] = {**FAKE_SANDBOX, "policy": agent.get("egress", [])}
    return out


def _member(lead: Obj, name: str) -> Obj:
    """A member of the lead's team; without a defer_tools of its own, it pins the lead's."""
    found = next(obj(m) for m in arr(lead["team"]) if obj(m)["name"] == name)
    own = obj(found.get("context", {}))
    if "defer_tools" in own:
        return found
    inherited = obj(lead.get("context", {})).get("defer_tools", "auto")
    return {**found, "context": {**own, "defer_tools": inherited}}


def _pin(agent: Obj, member: bool, choice: Obj | None) -> Obj:
    """thread_started's data, as agent() pins it (as a team member's with `member`; as a dynamic
    member's with its starter's `choice`: the chosen model and tools, the written block last)."""
    tools = _tools(agent, member)
    instructions = _instructions(agent)
    hashed = _hashed(agent)
    if "models" in agent:
        define = obj(choice["define"]) if choice is not None else {}
        models = {text(obj(m)["key"]): obj(m) for m in arr(agent["models"])}
        key = text(define["model"]) if "model" in define else next(iter(models))
        agent = {**agent, "model": models[key]}
    if choice is not None:
        define = obj(choice["define"])
        kept = {*KEPT, *(text(t) for t in arr(define["tools"]))}
        tools = [t for t in tools if text(obj(t)["name"]) in kept]
        if "instructions" in define:
            written = block(text(choice["starter"]), text(define["instructions"]))
            instructions = f"{instructions}\n\n{written}"
        hashed["dynamic"] = {"template": agent["name"], "define": define}
    first = obj(agent.get("model", {"name": "scripted-1"}))
    data: Obj = {
        "agent_name": agent["name"],
        "instructions": instructions,
        **_settings(first),
        "tools": tools,
        "policy": _policy(agent),
        **({"sandbox_provider": "fake"} if "sandbox" in agent else {}),
    }
    return {**data, "config_hash": sha(canonical({**data, **hashed}))}


def _started(agent: Obj, member: str | None, choice: Obj | None) -> Obj:
    return (
        _pin(agent, False, None) if member is None else _pin(_member(agent, member), True, choice)
    )


def _vector() -> str:
    rows: list[JsonValue] = [
        {
            "name": name,
            "agent": agent,
            **({} if member is None else {"member": member}),
            **({} if choice is None else {"dynamic": choice}),
            "thread_started": _started(agent, member, choice),
        }
        for name, agent, member, choice in cases()
    ]
    doc: Obj = {
        "description": (
            "Agent pins (spec/schema/README.md, The pinned config). Build `agent`: models are "
            "scripted models under `name`, declaring `price` and a `cache_ttl_ms` lifetime when "
            "given ('none' otherwise; `model` absent: scripted-1); each given permissions, retry "
            "or context section is the default one with those fields replaced; `sandbox` is the "
            "fake sandbox, with the agent's `egress` when given; `memory_write` gives local "
            "memory; extension hooks and observers are no-ops; an agent with `models` is a "
            "dynamic agent (its keys in order, the first the default), pinned as the member "
            "`dynamic` defines when given. A new thread's thread_started data (with `member`, "
            "that agent of the lead's team pinned as a member, which pins the lead's resolved "
            "defer_tools unless it sets its own) must equal `thread_started`, config_hash "
            "included; a team lead's `team` ids are fresh per thread and left out."
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
