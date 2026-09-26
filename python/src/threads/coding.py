"""`from threads.coding import CODING_INSTRUCTIONS, coding_agent` (extra `coding`).

`coding_agent()` is `agent()` with five options already chosen: Claude Sonnet 5, a keyless Docker
sandbox, `accept_edits` plus `bash(*)`, `local_memory()` and `CODING_INSTRUCTIONS`. It has no
runtime concept of its own, so a thread it starts and a thread from the equivalent `agent()` call
pin the same `config_hash`.

The option TypedDicts mirror `agent()`'s, key for key, with `model` not required (the preset has
one); `tests/coding/test_options.py` compares the key sets, so they can't drift apart.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Required, TypedDict, Unpack, overload

from pydantic import BaseModel

from threads.agents.agent import Agent
from threads.agents.bindings import AppTool, ToolServer
from threads.agents.factory import CommonOptions, agent
from threads.agents.sections import Section
from threads.agents.team_agent import TeamAgent, TeamLimits
from threads.anthropic import anthropic
from threads.docker import docker
from threads.hooks.extension import Extension
from threads.log import Permissions
from threads.loop.model import Model
from threads.memory.local_memory import local_memory

if TYPE_CHECKING:
    from threads.agents.dynamic_agent import DynamicAgent

__all__ = [
    "CODING_INSTRUCTIONS",
    "CodingAgentOptions",
    "CodingOutputOptions",
    "CodingServerOptions",
    "CodingServerOutputOptions",
    "CodingTeamOptions",
    "CodingTeamOutputOptions",
    "CodingTeamServerOptions",
    "CodingTeamServerOutputOptions",
    "CodingTeamToolOptions",
    "CodingTeamToolOutputOptions",
    "CodingToolOptions",
    "CodingToolOutputOptions",
    "coding_agent",
]

CODING_INSTRUCTIONS = (
    "You are a software engineer working in a sandbox. /workspace is a copy of the user's files; "
    "your edits stay in the sandbox. Before your first edit, record a baseline in /workspace: "
    "`git init -q; git add -A && git -c user.name=threads -c user.email=threads@localhost commit "
    "-q --no-verify --allow-empty -m threads-baseline && git tag -f threads-baseline`. Explore "
    "before you change anything: read the relevant files and run the existing tests. Plan "
    "multi-step work with todo_write and keep it current. Make the smallest change that solves "
    "the task, then verify it by running the tests or the program. Save to memory only when the "
    "user asks you to. End your answer with what you changed, what you verified and anything you "
    "could not do, followed by the output of `git add -A && git add -A --renormalize && git "
    "diff --cached --binary threads-baseline`."
)
"""The instructions the preset pins when the caller passes none, byte for byte
spec/conformance/vectors/coding-instructions.json, which the TypeScript preset pins too. Extend
it rather than replace it: the baseline commit and the closing `git diff` are how the edits leave
the sandbox."""

MODEL = "claude-sonnet-5"
"""Keyed from ANTHROPIC_API_KEY on the host when the run is set up, never in the sandbox."""

PERMISSIONS: Section[Permissions] = {"mode": "accept_edits", "allow": ["bash(*)"]}
"""Edits and commands run inside the Docker sandbox, which isolates them (no network, no bind
mounts, every capability dropped, uid 1000, a read-only root), so `bash(*)` says where the
boundary is rather than trusting the model. Memory writes, host-side tools and the protected
paths still ask, and the .threads guard and deny rules are still consulted first."""


class _Common(CommonOptions, total=False):
    """`agent()`'s `_AgentCommon`, with `model` not required."""

    model: Model
    subagents: "Sequence[Agent[None, object]]"
    """Agents spawn_agent may start, by name; the team tools come with them."""
    handoffs: "Sequence[Agent[None, object]]"
    """Agents this one may hand the conversation to, pinned as policy.handoffs."""


class _Team(TypedDict, total=False):
    """`agent()`'s team keys. Its own mixin is private, so this one mirrors it."""

    team: Required["Sequence[Agent[None, object] | DynamicAgent[None, object]]"]
    """Agents this one may start as team members, with the team tools. Even [] makes a TeamAgent,
    whose run() result carries the team."""
    team_limits: TeamLimits
    """The team's limits: 4 running members and 100 pending mails per member by default."""


class CodingAgentOptions(_Common, total=False):
    extensions: Sequence[Extension[None]]
    """Instructions, hooks and tools, run in this order; they read no deps."""


class CodingServerOptions(CodingAgentOptions, total=False):
    tools: Required[Sequence[ToolServer]]
    """MCP servers only: their tools need no deps."""


class CodingToolOptions[D](_Common, total=False):
    tools: Sequence[AppTool[D] | ToolServer]
    """App tools and MCP servers (`threads.mcp.mcp`)."""
    extensions: Sequence[Extension[D]]
    """Instructions, hooks and tools, run in this order; their contexts carry the run's deps."""


class CodingOutputOptions[O: BaseModel](CodingAgentOptions, total=False):
    output: Required[type[O]]
    """The structured final output. It replaces the final text, which is where the preset's
    instructions put the diff, so the diff has to be in the schema."""


class CodingServerOutputOptions[O: BaseModel](CodingOutputOptions[O], total=False):
    tools: Required[Sequence[ToolServer]]


class CodingToolOutputOptions[D, O: BaseModel](CodingToolOptions[D], total=False):
    output: Required[type[O]]


class CodingTeamOptions(CodingAgentOptions, _Team, total=False):
    pass


class CodingTeamServerOptions(CodingServerOptions, _Team, total=False):
    pass


class CodingTeamToolOptions[D](CodingToolOptions[D], _Team, total=False):
    pass


class CodingTeamOutputOptions[O: BaseModel](CodingOutputOptions[O], _Team, total=False):
    pass


class CodingTeamServerOutputOptions[O: BaseModel](CodingServerOutputOptions[O], _Team, total=False):
    pass


class CodingTeamToolOutputOptions[D, O: BaseModel](
    CodingToolOutputOptions[D, O], _Team, total=False
):
    pass


class _Options[D, O: BaseModel](CodingToolOptions[D], total=False):
    output: type[O]
    team: "Sequence[Agent[None, object] | DynamicAgent[None, object]]"
    team_limits: TeamLimits


def _with_defaults[D, O: BaseModel](overrides: _Options[D, O]) -> _Options[D, O]:
    """The preset's five keys under whatever the caller passed. `memory` and `sandbox` are read
    with `in`, not `get`, so passing None removes them instead of restoring the default."""
    return {
        **overrides,
        "model": overrides.get("model") or anthropic(MODEL),
        "instructions": overrides.get("instructions", CODING_INSTRUCTIONS),
        "permissions": overrides.get("permissions", PERMISSIONS),
        "memory": overrides["memory"] if "memory" in overrides else local_memory(),
        "sandbox": overrides["sandbox"]
        if "sandbox" in overrides
        else docker(cpus=2, memory_mb=4096),
    }


@overload
def coding_agent(**overrides: Unpack[CodingTeamOptions]) -> TeamAgent[None, str]: ...
@overload
def coding_agent(**overrides: Unpack[CodingTeamServerOptions]) -> TeamAgent[None, str]: ...
@overload
def coding_agent[D](**overrides: Unpack[CodingTeamToolOptions[D]]) -> TeamAgent[D, str]: ...
@overload
def coding_agent[O: BaseModel](
    **overrides: Unpack[CodingTeamOutputOptions[O]],
) -> TeamAgent[None, O]: ...
@overload
def coding_agent[O: BaseModel](
    **overrides: Unpack[CodingTeamServerOutputOptions[O]],
) -> TeamAgent[None, O]: ...
@overload
def coding_agent[D, O: BaseModel](
    **overrides: Unpack[CodingTeamToolOutputOptions[D, O]],
) -> TeamAgent[D, O]: ...
@overload
def coding_agent(**overrides: Unpack[CodingAgentOptions]) -> Agent[None, str]: ...
@overload
def coding_agent(**overrides: Unpack[CodingServerOptions]) -> Agent[None, str]: ...
@overload
def coding_agent[D](**overrides: Unpack[CodingToolOptions[D]]) -> Agent[D, str]: ...
@overload
def coding_agent[O: BaseModel](**overrides: Unpack[CodingOutputOptions[O]]) -> Agent[None, O]: ...
@overload
def coding_agent[O: BaseModel](
    **overrides: Unpack[CodingServerOutputOptions[O]],
) -> Agent[None, O]: ...
@overload
def coding_agent[D, O: BaseModel](
    **overrides: Unpack[CodingToolOutputOptions[D, O]],
) -> Agent[D, O]: ...
def coding_agent[D, O: BaseModel](
    **overrides: Unpack[_Options[D, O]],
) -> Agent[D, str] | Agent[D, O] | TeamAgent[D, str] | TeamAgent[D, O]:
    """spec/api.json `coding_agent`. Pure, like `agent()`: no I/O, no env read, no socket. Raises
    ConfigError for the same reasons `agent()` does."""
    return agent(**_with_defaults(overrides))
