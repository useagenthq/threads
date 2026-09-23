"""The built-in tools' pinned specs: names, descriptions and input schemas come
from the shared catalog (spec/schema/tools.v1.catalog.json, generated into `tools_v1`), and the
generated input models parse the model's arguments. Only the effect class is decided here."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, TypedDict

from pydantic import JsonValue, TypeAdapter

from threads._generated import tools_v1
from threads._strict_model import StrictModel
from threads.log import EffectClass, ToolSpec


class _Entry(TypedDict):
    name: str
    description: str
    input_schema: dict[str, JsonValue]


_ENTRIES: Final = TypeAdapter(list[_Entry]).validate_python(json.loads(tools_v1.TOOL_CATALOG))

MODELS: Final[Mapping[str, type[StrictModel]]] = {
    "bash": tools_v1.BashInput,
    "edit": tools_v1.EditInput,
    "glob": tools_v1.GlobInput,
    "grep": tools_v1.GrepInput,
    "handoff": tools_v1.HandoffInput,
    "ls": tools_v1.LsInput,
    "read": tools_v1.ReadInput,
    "read_tool_result": tools_v1.ReadToolResultInput,
    "send_message": tools_v1.SendMessageInput,
    "spawn_agent": tools_v1.SpawnAgentInput,
    "team_task_claim": tools_v1.TeamTaskClaimInput,
    "team_task_create": tools_v1.TeamTaskCreateInput,
    "team_task_update": tools_v1.TeamTaskUpdateInput,
    "todo_write": tools_v1.TodoWriteInput,
    "write": tools_v1.WriteInput,
}
_EFFECTS: Final[Mapping[str, EffectClass]] = {
    "bash": "sandbox_local",
    "edit": "sandbox_local",
    "glob": "read_only",
    "grep": "read_only",
    "ls": "read_only",
    "read": "read_only",
    "read_tool_result": "read_only",
    "write": "sandbox_local",
}
HOST: Final = frozenset({"read_tool_result"})
"""Built-ins that run on the host: offered with or without a sandbox."""
TEAM: Final = frozenset({"send_message", "team_task_claim", "team_task_create", "team_task_update"})
"""Offered to a team: an agent with subagents, and every child it spawns."""
FRAMEWORK: Final = TEAM | {"todo_write", "handoff", "spawn_agent"}
"""Log-only tools: `read_only` is exact, since only log state changes."""
NAMES: Final = frozenset(MODELS)
SANDBOXED: Final = NAMES - HOST - FRAMEWORK
PROVIDED: Final[Mapping[str, type[StrictModel]]] = {
    "forget_memory": tools_v1.ForgetMemoryInput,
    "save_memory": tools_v1.SaveMemoryInput,
    "search_knowledge": tools_v1.SearchKnowledgeInput,
    "search_memory": tools_v1.SearchMemoryInput,
}
"""Host tools pinned only with a memory or knowledge provider."""
_WRITES: Final = frozenset({"save_memory", "forget_memory"})


@dataclass(frozen=True, slots=True)
class Writes:
    """The effect class a memory provider declares for its writes. A provider that
    declares nothing is unguarded: an uncertain write parks, it is never retried blindly."""

    effect: EffectClass = "unguarded"
    dedup_window_ms: int | None = None


def specs(
    *,
    sandbox: bool,
    egress_denied: bool,
    memory: Writes | None = None,
    knowledge: bool = False,
    spawn: bool = False,
    team: bool = False,
    handoffs: bool = False,
) -> tuple[ToolSpec, ...]:
    """The pinned built-ins, sorted by name. bash is sandbox_local only under deny-all egress;
    with any outbound path a command may change state elsewhere. todo_write
    is always offered; spawn_agent with subagents, the team tools in a team, handoff with
    handoff targets."""
    wanted = (("todo_write", True), ("spawn_agent", spawn), ("handoff", handoffs))
    offered: set[str] = {name for name, on in wanted if on} | (TEAM if team else frozenset())
    out: list[ToolSpec] = []
    for entry in _ENTRIES:
        name = entry["name"]
        if name in FRAMEWORK:
            effect = "read_only" if name in offered else None
        else:
            effect = _effect(name, sandbox=sandbox, egress_denied=egress_denied, memory=memory)
        if effect is None or (name == "search_knowledge" and not knowledge):
            continue  # a capability that isn't configured
        data: dict[str, JsonValue] = {
            "name": name,
            "description": entry["description"],
            "input_schema": entry["input_schema"],
            "effect_class": effect,
        }
        if effect == "idempotent" and memory is not None and name in _WRITES:
            data["dedup_window_ms"] = memory.dedup_window_ms
        out.append(ToolSpec.model_validate(data))
    return tuple(out)


def _effect(
    name: str, *, sandbox: bool, egress_denied: bool, memory: Writes | None
) -> EffectClass | None:
    if name == "search_knowledge":
        return "read_only"
    if name in PROVIDED:
        if memory is None:
            return None
        return memory.effect if name in _WRITES else "read_only"
    if name not in NAMES or (name not in HOST and not sandbox):
        return None
    return "unguarded" if name == "bash" and not egress_denied else _EFFECTS[name]
