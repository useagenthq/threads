"""Drift (spec lane 22, B.3): the case's recorded config against the agent as it is pinned now, by
a dry pin that runs no setup. The recorded prompt is line 0 of the case's first request; the model
is the case's first settings epoch. What the dry pin can't see is `unchecked`, never stale."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue

from threads.agents.definition import DryPin
from threads.evals.compare import canonical

type Started = Mapping[str, JsonValue]
"""A thread_started's data as wire JSON, recorded or freshly pinned."""

_RELATIONS: Final[Mapping[str, str]] = {
    "subagent": "spawn",
    "team_member": "team_member",
    "handoff": "handoff",
}


@dataclass(frozen=True, slots=True)
class Recorded:
    started: Started
    """The case log's thread_started: the first settings epoch and the pinned tools."""
    line0: bytes | None
    """line0.json (or a recorded request's line 0), when the case has one."""


@dataclass(frozen=True, slots=True)
class DriftResult:
    """The report's drift check, as fields in its key order."""

    ok: bool
    kinds: tuple[str, ...]
    tools: Mapping[str, tuple[str, ...]] | None = None
    unchecked: tuple[str, ...] | None = None
    agents: tuple[str, ...] | None = None

    def to_json(self) -> JsonValue:
        out: dict[str, JsonValue] = {"ok": self.ok, "kinds": list(self.kinds)}
        if self.tools is not None:
            out["tools"] = {k: list(v) for k, v in self.tools.items()}
        if self.unchecked is not None:
            out["unchecked"] = list(self.unchecked)
        if self.agents is not None:
            out["agents"] = list(self.agents)
        return out


def _tools(started: Started) -> list[Mapping[str, JsonValue]]:
    tools = started.get("tools")
    return [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []


def _shape(spec: Mapping[str, JsonValue]) -> str:
    """A pinned tool as drift compares it: everything but where it came from."""
    return canonical({k: v for k, v in spec.items() if k != "origin"})


def _origin(spec: Mapping[str, JsonValue]) -> str:
    origin = spec.get("origin")
    extension = origin.get("extension") if isinstance(origin, dict) else None
    return extension if isinstance(extension, str) else ""


def _recorded_system(line0: bytes | None) -> str | None:
    """The recorded line 0's system text, or None when there is no readable line 0."""
    if line0 is None:
        return None
    try:
        parsed: JsonValue = json.loads(line0)
    except ValueError:
        return None
    system = parsed.get("system") if isinstance(parsed, dict) else None
    return system if isinstance(system, str) else None


def _unchecked(pin: DryPin) -> list[str]:
    return [
        *(f"mcp:{s}" for s in pin.mcp),
        *(f"extension:{e}" for e in pin.setup_extensions),
        *pin.setup_providers,
    ]


def _comparable(started: Started, pin: DryPin) -> dict[str, str]:
    """Tools the dry pin can compare: no MCP tool, none a setup-bearing extension contributed."""
    out: dict[str, str] = {}
    for t in _tools(started):
        name = str(t.get("name"))
        mcp = any(name.startswith(f"mcp__{s}__") for s in pin.mcp)
        if not mcp and _origin(t) not in pin.setup_extensions:
            out[name] = _shape(t)
    return out


def _tool_diff(recorded: Started, pin: DryPin) -> Mapping[str, tuple[str, ...]] | None:
    before, after = _comparable(recorded, pin), _comparable(pin.started, pin)
    added = tuple(sorted(n for n in after if n not in before))
    removed = tuple(sorted(n for n in before if n not in after))
    changed = tuple(sorted(n for n, s in after.items() if n in before and before[n] != s))
    if not (added or removed or changed):
        return None
    return {"added": added, "removed": removed, "changed": changed}


def _originless(recorded: Started, pin: DryPin) -> bool:
    """A case saved before tool origins can't tell a setup-bearing extension's tools apart."""
    return bool(pin.setup_extensions) and not any("origin" in t for t in _tools(recorded))


def _settings(s: Started) -> str:
    return canonical([s.get("model"), s.get("model_params"), s.get("adapter")])


def _compare(recorded: Recorded, pin: DryPin) -> DriftResult:
    unchecked = _unchecked(pin)
    system = _recorded_system(recorded.line0)
    if system is None:
        unchecked.append("no_recorded_prefix")
    kinds: list[str] = []
    blind = bool(pin.setup_extensions or pin.setup_providers)
    if not blind and system is not None and system != pin.started.get("instructions"):
        kinds.append("prompt")
    tools = None if _originless(recorded.started, pin) else _tool_diff(recorded.started, pin)
    if tools is not None:
        kinds.append("tools")
    if _settings(recorded.started) != _settings(pin.started):
        kinds.append("model")
    same_hash = recorded.started.get("config_hash") == pin.started.get("config_hash")
    if not kinds and not unchecked and not same_hash:
        kinds.append("config")
    return DriftResult(not kinds, tuple(kinds), tools, tuple(unchecked) if unchecked else None)


def drift(recorded: Recorded, pins: Sequence[DryPin]) -> DriftResult:
    """The drift check of one case against the agents given."""
    parent = recorded.started.get("parent")
    relation = parent.get("relation") if isinstance(parent, dict) else None
    if isinstance(relation, str):
        return DriftResult(True, (), unchecked=(f"relation:{_RELATIONS[relation]}",))
    name = recorded.started.get("agent_name")
    pin = next((p for p in pins if p.started.get("agent_name") == name), None)
    if pin is None:
        return DriftResult(False, (), agents=tuple(str(p.started.get("agent_name")) for p in pins))
    return _compare(recorded, pin)
