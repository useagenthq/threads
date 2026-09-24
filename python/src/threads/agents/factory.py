"""`agent()` (spec/api.json): a pure definition, run with `run()` or `stream()`; with a team, a
`TeamAgent`. `agent()` does no I/O, reads no env and opens no sockets."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Final, Literal, Required, TypedDict, Unpack, overload

from pydantic import BaseModel

from threads._json_schema import unchecked
from threads.agents import narrowing
from threads.agents.agent import Agent
from threads.agents.bindings import DEFAULT_PERMISSIONS, AppTool, ToolServer
from threads.agents.builtins import Egress
from threads.agents.catalog import GitOptions, LspOptions, WebOptions, catalog
from threads.agents.config import ConfigError
from threads.agents.definition import Definition
from threads.agents.deps import needs_no_deps
from threads.agents.sections import Section, completed
from threads.agents.skills import Skill, checked
from threads.agents.team_agent import TeamAgent, TeamLimits
from threads.agents.tool import json_schema
from threads.hooks.extension import Extension
from threads.log import Budget, Context, Permissions, Principal, Retry
from threads.loop.defaults import CONTEXT, RETRY
from threads.loop.model import Model
from threads.memory.authority import MemoryWrite
from threads.memory.protocol import KnowledgeProvider, MemoryProvider
from threads.sandbox.protocol import Sandbox
from threads.team.ops import TeamLimits as Limits

if TYPE_CHECKING:
    from threads.agents.dynamic_agent import DynamicAgent


class CommonOptions(TypedDict, total=False):
    """agent()'s options that dynamic_agent() takes too: all but model and the team's."""

    instructions: str
    name: str
    permissions: Section[Permissions]
    """A complete Permissions, or only the fields to change over the defaults."""
    budget: Budget
    on_unknown_usage: Literal["upper_bound", "stop"]
    """"stop": a limit the model has no per-attempt bound for is refused at run time instead of
    at setup (budget_unenforceable); default "upper_bound"."""
    retry: Section[Retry]
    """A complete Retry, or only the fields to change over the defaults."""
    context: Section[Context]
    """The context ladder's settings, complete or only the fields to change; absent: the ADR
    defaults."""
    fallback: Sequence[Model]
    """Models to fall back to, in order, when the current one stays overloaded. With the default
    fallback_scope "turn", the next input reverts to the settings before the fallback."""
    output_retries: int
    """Failed candidates per turn (a rejected final_output or a plain-text end) before the run
    fails output_invalid; default 2. A non-negative integer."""
    sandbox: Sandbox
    """Absent: no sandbox tools. Present: bash, read, write, edit, ls, glob and grep."""
    egress: Egress
    """Sandbox egress allowlist; [] (the default) is deny-all."""
    memory: MemoryProvider
    """save_memory, search_memory and forget_memory."""
    memory_write: MemoryWrite
    """Write authority for save_memory and forget_memory; default "ask"."""
    knowledge: KnowledgeProvider
    """search_knowledge over host-ingested sources."""
    skills: Sequence[Skill]
    """Host-pinned skills: listed in line 0, a body loaded on demand."""
    web: WebOptions
    """Host-side web_fetch and web_search."""
    git: GitOptions
    """The git gateway tools, with the forge credential kept on the host."""
    computer: bool
    """computer_screenshot and computer; needs a sandbox with a desktop."""
    lsp: LspOptions
    """lsp for these languages, served by the sandbox image."""
    output_styles: Mapping[str, str]
    """Named instructions an operator can switch a thread to with Thread.set_output_style.
    Pinned with the config; keys and texts non-empty."""
    approvers: Sequence[Principal]
    """Who may answer approval challenges and resolve parked effects for runs this agent roots,
    its subagents and handoff targets included. Unset: the root run's originating principal
    (spec/schema/README.md, Approval authority)."""


class _AgentCommon(CommonOptions, total=False):
    model: Required[Model]
    subagents: "Sequence[Agent[None, object]]"
    """Agents spawn_agent may start, by name; the team tools come with them. A structured
    subagent reports the canonical JSON of its output."""
    handoffs: "Sequence[Agent[None, object]]"
    """Agents this one may hand the conversation to, pinned as policy.handoffs."""


class AgentOptions(_AgentCommon, total=False):
    extensions: Sequence[Extension[None]]
    """Instructions, hooks and tools, run in this order; they read no deps."""


class ServerAgentOptions(AgentOptions, total=False):
    tools: Required[Sequence[ToolServer]]
    """MCP servers only: their tools need no deps."""


class ToolAgentOptions[D](_AgentCommon, total=False):
    tools: Sequence[AppTool[D] | ToolServer]
    """App tools and MCP servers (`threads.mcp.mcp`)."""
    extensions: Sequence[Extension[D]]
    """Instructions, hooks and tools, run in this order; their contexts carry the run's deps."""


class OutputAgentOptions[O: BaseModel](AgentOptions, total=False):
    output: Required[type[O]]
    """The structured final output: the model returns it through final_output, checked strictly
    against this model, and a completed run's output is an O."""


class ServerOutputAgentOptions[O: BaseModel](OutputAgentOptions[O], total=False):
    tools: Required[Sequence[ToolServer]]


class ToolOutputAgentOptions[D, O: BaseModel](ToolAgentOptions[D], total=False):
    output: Required[type[O]]


class _Team(TypedDict, total=False):
    team: Required["Sequence[Agent[None, object] | DynamicAgent[None, object]]"]
    """Agents this one may start as team members, with the team tools. Even [] makes a TeamAgent,
    whose run() result carries the team."""
    team_limits: TeamLimits
    """The team's limits: 4 running members and 100 pending mails per member by default."""


class TeamAgentOptions(AgentOptions, _Team, total=False):
    pass


class TeamServerAgentOptions(ServerAgentOptions, _Team, total=False):
    pass


class TeamToolAgentOptions[D](ToolAgentOptions[D], _Team, total=False):
    pass


class TeamOutputAgentOptions[O: BaseModel](OutputAgentOptions[O], _Team, total=False):
    pass


class TeamServerOutputAgentOptions[O: BaseModel](ServerOutputAgentOptions[O], _Team, total=False):
    pass


class TeamToolOutputAgentOptions[D, O: BaseModel](ToolOutputAgentOptions[D, O], _Team, total=False):
    pass


class _Options[D](ToolAgentOptions[D], total=False):
    output: object
    team: "Sequence[Agent[None, object] | DynamicAgent[None, object]]"
    team_limits: TeamLimits


def _text(text: str) -> str:
    return text


@overload
def agent(**options: Unpack[TeamAgentOptions]) -> TeamAgent[None, str]: ...
@overload
def agent(**options: Unpack[TeamServerAgentOptions]) -> TeamAgent[None, str]: ...
@overload
def agent[D](**options: Unpack[TeamToolAgentOptions[D]]) -> TeamAgent[D, str]: ...
@overload
def agent[O: BaseModel](**options: Unpack[TeamOutputAgentOptions[O]]) -> TeamAgent[None, O]: ...
@overload
def agent[O: BaseModel](
    **options: Unpack[TeamServerOutputAgentOptions[O]],
) -> TeamAgent[None, O]: ...
@overload
def agent[D, O: BaseModel](
    **options: Unpack[TeamToolOutputAgentOptions[D, O]],
) -> TeamAgent[D, O]: ...
@overload
def agent(**options: Unpack[AgentOptions]) -> Agent[None, str]: ...
@overload
def agent(**options: Unpack[ServerAgentOptions]) -> Agent[None, str]: ...
@overload
def agent[D](**options: Unpack[ToolAgentOptions[D]]) -> Agent[D, str]: ...
@overload
def agent[O: BaseModel](**options: Unpack[OutputAgentOptions[O]]) -> Agent[None, O]: ...
@overload
def agent[O: BaseModel](**options: Unpack[ServerOutputAgentOptions[O]]) -> Agent[None, O]: ...
@overload
def agent[D, O: BaseModel](**options: Unpack[ToolOutputAgentOptions[D, O]]) -> Agent[D, O]: ...
def agent[D](**options: Unpack[_Options[D]]) -> Agent[D, object] | Agent[None, object]:
    """spec/api.json `agent`. Pure: no I/O. Raises ConfigError for duplicate tool names, an
    output that is not a Pydantic model class, or output_retries that is not a non-negative
    integer."""
    _no_templates(options)
    output = output_model(options.get("output"))
    decode: Callable[[str], object] = _text
    if output is not None:
        decode = partial(output.model_validate_json, strict=True)
    given = options.get("tools", ())
    servers = tuple(t for t in given if isinstance(t, ToolServer))
    tools = tuple(t for t in given if not isinstance(t, ToolServer))
    team = None if "team" not in options else tuple(a.definition for a in options["team"])
    links = Links(
        team,
        Limits(**options.get("team_limits", {})),
        tuple(a.definition for a in options.get("subagents", ())),
        tuple(a.definition for a in options.get("handoffs", ())),
    )
    extensions = tuple(options.get("extensions", ()))
    given = ((tools, servers), extensions)
    definition = build_definition(options, options["model"], given, output, links)
    if needs_no_deps(definition):
        if team is None:
            return Agent(definition, (None,), decode)
        return TeamAgent(definition, (None,), decode)
    return Agent(definition, (), decode) if team is None else TeamAgent(definition, (), decode)


def _no_templates(options: _AgentCommon) -> None:
    """A dynamic agent runs only as a team member: never a subagent or a handoff target."""
    for key in ("subagents", "handoffs"):
        found = next((a for a in options.get(key, ()) if a.definition.models), None)
        if found is not None:
            why = f"dynamic agents run as team members; put {found.name} in team"
            raise ConfigError("invalid_config", why)


def output_model(output: object) -> type[BaseModel] | None:
    """A Pydantic model class whose JSON Schema the log can check in full (semantic rule 20):
    anything else is refused here, never by a run that has an answer to record."""
    if output is None:
        return None
    if not (isinstance(output, type) and issubclass(output, BaseModel)):
        why = f"output must be a Pydantic model class, got {output!r}"
        raise ConfigError("invalid_config", why)
    why = unchecked(json_schema(output))
    if why is not None:
        hint = "use a bound, a length, a pattern or an enum instead"
        raise ConfigError("invalid_config", f"output {output.__name__}: {why}; {hint}")
    return output


_MAX_SAFE: Final = 2**53 - 1
"""The largest integer the wire carries (spec/schema/README.md, wire rule 4)."""


def _retries(options: CommonOptions) -> int:
    retries = options.get("output_retries", 2)
    # bool is an int subclass, and True is not a retry count; the pin is a wire integer.
    if type(retries) is not int or not 0 <= retries <= _MAX_SAFE:
        why = f"output_retries must be an integer from 0 to 2**53 - 1, got {retries!r}"
        raise ConfigError("invalid_config", why)
    return retries


def _styles(styles: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(_style(name, text) for name, text in styles.items())


def _style(name: object, text: object) -> tuple[str, str]:
    # Checked at runtime: the pin is wire JSON whatever the caller's types said.
    if not (isinstance(name, str) and name and isinstance(text, str) and text):
        why = f"output_styles[{name!r}] needs a non-empty name and a non-empty text"
        raise ConfigError("invalid_config", why)
    return name, text


@dataclass(frozen=True, slots=True)
class Links:
    """The agents a definition may start or hand off to, and its team's limits."""

    team: tuple[Definition[None], ...] | None = None
    limits: Limits = field(default_factory=Limits)
    subagents: tuple[Definition[None], ...] = ()
    handoffs: tuple[Definition[None], ...] = ()


def build_definition[T](
    options: CommonOptions,
    model: Model,
    given: tuple[tuple[tuple[AppTool[T], ...], tuple[ToolServer, ...]], tuple[Extension[T], ...]],
    output: type[BaseModel] | None,
    links: Links,
) -> Definition[T]:
    """The definition agent() and dynamic_agent() describe; raises ConfigError."""
    (tools, servers), extensions = given
    sandbox = options.get("sandbox")
    egress = options.get("egress", ())
    if egress != "unenforced" and egress:
        raise ConfigError("egress_policy_unsupported", "egress host allowlists aren't supported")
    if sandbox is not None and sandbox.info.egress == "unenforced" and egress != "unenforced":
        # A provider that can't enforce egress needs an explicit opt-in.
        raise ConfigError("egress_policy_unsupported", f"{sandbox.info.provider}: egress")
    definition = Definition(
        options.get("name", "agent"),
        model,
        options.get("instructions", ""),
        tools,
        completed("permissions", options.get("permissions"), DEFAULT_PERMISSIONS),
        options.get("budget"),
        completed("retry", options.get("retry"), RETRY),
        completed("context", options.get("context"), CONTEXT),
        sandbox,
        egress,
        extensions,
        options.get("memory"),
        options.get("memory_write", "ask"),
        options.get("knowledge"),
        servers,
        tuple(replace(a, member=True) for a in links.subagents),
        links.handoffs,
        team=links.team,
        team_limits=links.limits,
        skills=checked(options.get("skills", ())),
        catalog=catalog(
            sandbox,
            web=options.get("web"),
            git=options.get("git"),
            computer=options.get("computer", False),
            lsp=options.get("lsp"),
        ),
        output=output,
        output_retries=_retries(options),
        fallback=tuple(options.get("fallback", ())),
        output_styles=_styles(options.get("output_styles", {})),
        context_set=_context_set(options.get("context")),
    )
    if "approvers" in options:
        definition = replace(definition, approvers=tuple(options["approvers"]))
    if "on_unknown_usage" in options:
        definition = replace(definition, on_unknown_usage=options["on_unknown_usage"])
    pinned = frozenset(s.name for s in definition.specs())
    defer = definition.defer_tools()
    # A child that sets no context pins its parent's resolved defer_tools (spec/schema/README.md).
    children = tuple(
        replace(c, allowed=pinned, inherited_defer=defer) for c in definition.subagents
    )
    handoffs = tuple(replace(h, inherited_defer=defer) for h in definition.handoffs)
    team_of = definition.team
    members = None if team_of is None else tuple(replace(m, inherited_defer=defer) for m in team_of)
    definition = replace(definition, subagents=children, handoffs=handoffs, team=members)
    narrowing.check(definition)
    narrowing.enforceable(definition)
    for kind, names in (
        ("tool", [s.name for s in definition.specs()]),
        ("MCP server", [s.name for s in servers]),
        ("extension", [e.name for e in definition.extensions]),
    ):
        if len(set(names)) != len(names):
            raise ConfigError("duplicate_name", f"{kind} names repeat: {names}")
    return definition


def _context_set(context: Section[Context] | None) -> frozenset[str] | None:
    """The fields a partial context names; None for a complete one (or none at all)."""
    return None if context is None or isinstance(context, BaseModel) else frozenset(context)
