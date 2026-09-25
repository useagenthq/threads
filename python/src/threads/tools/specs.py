"""The built-in tools' pinned specs: names, descriptions and input schemas come
from the shared catalog (spec/schema/tools.v1.catalog.json, generated into `tools_v1`), and the
generated input models parse the model's arguments. Only the effect class is decided here."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, TypedDict

from pydantic import JsonValue, TypeAdapter

from threads._generated import tools_v1
from threads._strict_model import StrictModel
from threads._tool_names import FRAMEWORK, PINNED_MEMBERS, SEARCH, TEAM
from threads.log import EffectClass, ToolSpec


class _Entry(TypedDict):
    name: str
    description: str
    input_schema: dict[str, JsonValue]


_ENTRIES: Final = TypeAdapter(list[_Entry]).validate_python(json.loads(tools_v1.TOOL_CATALOG))

MODELS: Final[Mapping[str, type[StrictModel]]] = {
    "ask": tools_v1.AskInput,
    "ask_user": tools_v1.AskUserInput,
    "bash": tools_v1.BashInput,
    "computer": tools_v1.ComputerInput,
    "computer_screenshot": tools_v1.ComputerScreenshotInput,
    "edit": tools_v1.EditInput,
    "git_clone": tools_v1.GitCloneInput,
    "git_fetch": tools_v1.GitFetchInput,
    "git_push": tools_v1.GitPushInput,
    "glob": tools_v1.GlobInput,
    "grep": tools_v1.GrepInput,
    "handoff": tools_v1.HandoffInput,
    "ls": tools_v1.LsInput,
    "load_skill": tools_v1.LoadSkillInput,
    "lsp": tools_v1.LspInput,
    "monitor": tools_v1.MonitorInput,
    "notebook_edit": tools_v1.NotebookEditInput,
    "open_pull_request": tools_v1.OpenPullRequestInput,
    "read": tools_v1.ReadInput,
    "read_tool_result": tools_v1.ReadToolResultInput,
    "reply": tools_v1.ReplyInput,
    "send": tools_v1.SendInput,
    "send_message": tools_v1.SendMessageInput,
    "spawn_agent": tools_v1.SpawnAgentInput,
    "start": tools_v1.StartInput,
    "team_task_claim": tools_v1.TeamTaskClaimInput,
    "team_task_create": tools_v1.TeamTaskCreateInput,
    "team_task_update": tools_v1.TeamTaskUpdateInput,
    "todo_write": tools_v1.TodoWriteInput,
    "wait": tools_v1.WaitInput,
    "web_fetch": tools_v1.WebFetchInput,
    "web_search": tools_v1.WebSearchInput,
    "write": tools_v1.WriteInput,
}
_EFFECTS: Final[Mapping[str, EffectClass]] = {
    "bash": "sandbox_local",
    # A click or keypress can submit a form: GUI actions are unguarded.
    "computer": "unguarded",
    "computer_screenshot": "read_only",
    "edit": "sandbox_local",
    "git_clone": "read_only",
    "git_fetch": "read_only",
    "git_push": "reconcilable",
    "glob": "read_only",
    "grep": "read_only",
    "ls": "read_only",
    "lsp": "read_only",
    "notebook_edit": "sandbox_local",
    "open_pull_request": "reconcilable",
    "read": "read_only",
    "read_tool_result": "read_only",
    "web_fetch": "read_only",
    "web_search": "read_only",
    "write": "sandbox_local",
}
HOST: Final = frozenset({"read_tool_result"})
"""Built-ins that run on the host: offered with or without a sandbox."""
SKILL: Final = "load_skill"
"""Host-side over the pinned skills; pinned only when the agent has skills."""
MEMBERS: Final = frozenset({"ask", "cancel", "monitor", "reply", "send", "start", "wait"})
"""A team's model tools (spec/schema/README.md, Teams): pinned for a lead and its members, so no
tool of a team's agent may take one of these names."""
WEB: Final = frozenset({"web_fetch", "web_search"})
"""Host tools through the host's fenced web transport."""
GIT: Final = frozenset({"git_clone", "git_fetch", "git_push", "open_pull_request"})
"""The host git gateway: host credentials, the sandbox's clone."""
GATED: Final = WEB | GIT | {"computer", "computer_screenshot", "lsp"}
"""Pinned only when their capability is configured."""
NAMES: Final = frozenset(MODELS)
SANDBOXED: Final = NAMES - HOST - FRAMEWORK - WEB - GIT - {SKILL}
SANDBOX_TOOLS: Final = frozenset(
    {"bash", "edit", "glob", "grep", "ls", "notebook_edit", "read", "write"}
)
"""The sandbox's own tools: what they change stays inside it, so a stub-mode run (a live eval,
a stub fork) runs them for real only in a sandbox whose egress is deny-all. The computer tool
is left out: it can act outside the sandbox."""
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


def search_tool_spec(deferred_names: Sequence[str]) -> ToolSpec:
    """tool_search as pinned: the catalog description, then the deferred names by code point.
    The pin, member rebind and the goldens all build it here."""
    entry = next(e for e in _ENTRIES if e["name"] == SEARCH)
    listed = ", ".join(sorted(deferred_names))
    data: dict[str, JsonValue] = {
        "name": SEARCH,
        "description": f"{entry['description']}\n\nDeferred tools (search to load): {listed}",
        "input_schema": entry["input_schema"],
        "effect_class": "read_only",
    }
    return ToolSpec.model_validate(data)


def agent_tools(
    *, spawn: bool, team: bool, handoffs: bool, members: bool = False, answerer: bool = False
) -> frozenset[str]:
    """spawn_agent with subagents, the task-board tools in a subagent team, handoff with handoff
    targets, the team tools for a lead (agent(team=...)) and its members, and ask_user when a
    host answers for the run (a channel conversation or an HTTP API call)."""
    wanted = (("spawn_agent", spawn), ("handoff", handoffs), ("ask_user", answerer))
    chosen = frozenset(n for n, on in wanted if on) | (
        PINNED_MEMBERS if members else frozenset[str]()
    )
    return chosen | TEAM if team else chosen


def specs(  # noqa: PLR0913 - one flag per configured capability
    *,
    sandbox: bool,
    egress_denied: bool,
    memory: Writes | None = None,
    knowledge: bool = False,
    framework: frozenset[str] = frozenset(),
    gated: frozenset[str] = frozenset(),
    skills: bool = False,
) -> tuple[ToolSpec, ...]:
    """The pinned built-ins, sorted by name. A sandbox_local built-in (bash, edit, notebook_edit,
    write) keeps its class only under deny-all egress; with any outbound path what it changes may
    reach elsewhere, so it is unguarded and an in-doubt call parks. todo_write
    is always offered; `framework` names the other framework tools this agent is offered, and
    `gated` the GATED tools whose capability is configured.
    load_skill needs `skills`."""
    offered = framework | {"todo_write"}
    out: list[ToolSpec] = []
    for entry in _ENTRIES:
        name = entry["name"]
        if name in FRAMEWORK:
            effect = "read_only" if name in offered else None
        else:
            effect = _effect(name, sandbox=sandbox, egress_denied=egress_denied, memory=memory)
        unset = (
            (name == "search_knowledge" and not knowledge)
            or (name == SKILL and not skills)
            or (name in GATED and name not in gated)
        )
        if effect is None or unset:
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
    if name in {"search_knowledge", SKILL}:
        return "read_only"
    if name in PROVIDED:
        if memory is None:
            return None
        return memory.effect if name in _WRITES else "read_only"
    if name not in NAMES or (name not in HOST | WEB and not sandbox):
        return None
    effect = _EFFECTS[name]
    return "unguarded" if effect == "sandbox_local" and not egress_denied else effect
