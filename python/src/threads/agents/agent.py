"""`agent()` (spec/api.json): a pure definition, run with `run()` or `stream()`.

`agent()` does no I/O, reads no env and opens no sockets; a run binds the
store, the principal and the deps.
"""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import replace
from functools import partial
from typing import Final, Literal, Required, TypedDict, TypeGuard, Unpack, overload

from pydantic import BaseModel

from threads._json_schema import unchecked
from threads._tool_deps import ContextDeps, context_deps, missing_deps
from threads.agents import narrowing
from threads.agents.bindings import AppTool, ToolServer
from threads.agents.builtins import Egress
from threads.agents.catalog import GitOptions, LspOptions, WebOptions, catalog
from threads.agents.config import ConfigError, Failure
from threads.agents.definition import Definition
from threads.agents.results import Completed, RunResult, StreamEvent
from threads.agents.run import (
    Emit,
    Input,
    RunOptions,
    RunOptionsWithDeps,
    execute,
    outside_any_branch,
    with_servers,
)
from threads.agents.setup import set_up
from threads.agents.skills import Skill, checked
from threads.agents.stream import RunStream
from threads.agents.tool import Tool, json_schema
from threads.hooks.extension import Extension
from threads.log import Budget, Context, Permissions, Principal, Retry
from threads.loop.model import Model
from threads.memory.authority import MemoryWrite
from threads.memory.protocol import KnowledgeProvider, MemoryProvider
from threads.result import Err, Ok
from threads.sandbox.protocol import Sandbox


class AgentOptions(TypedDict, total=False):
    model: Required[Model]
    instructions: str
    name: str
    permissions: Permissions
    budget: Budget
    on_unknown_usage: Literal["upper_bound", "stop"]
    """"stop": a limit the model has no per-attempt bound for is refused at run time instead of
    at setup (budget_unenforceable); default "upper_bound"."""
    retry: Retry
    context: Context
    """The context ladder's settings; absent: the ADR defaults."""
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
    extensions: Sequence[Extension]
    """Instructions and hooks, run in this order."""
    memory: MemoryProvider
    """save_memory, search_memory and forget_memory."""
    memory_write: MemoryWrite
    """Write authority for save_memory and forget_memory; default "ask"."""
    knowledge: KnowledgeProvider
    """search_knowledge over host-ingested sources."""
    skills: Sequence[Skill]
    """Host-pinned skills: listed in line 0, a body loaded on demand."""
    subagents: "Sequence[Agent[None, object]]"
    """Agents spawn_agent may start, by name; the team tools come with them. A structured
    subagent reports the canonical JSON of its output."""
    handoffs: "Sequence[Agent[None, object]]"
    """Agents this one may hand the conversation to, pinned as policy.handoffs."""
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


class ServerAgentOptions(AgentOptions, total=False):
    tools: Required[Sequence[ToolServer]]
    """MCP servers only: their tools need no deps."""


class ToolAgentOptions[D](AgentOptions, total=False):
    tools: Required[Sequence[AppTool[D] | ToolServer]]
    """App tools and MCP servers (`threads.mcp.mcp`)."""


class OutputAgentOptions[O: BaseModel](AgentOptions, total=False):
    output: Required[type[O]]
    """The structured final output: the model returns it through final_output, checked strictly
    against this model, and a completed run's output is an O."""


class ServerOutputAgentOptions[O: BaseModel](OutputAgentOptions[O], total=False):
    tools: Required[Sequence[ToolServer]]


class ToolOutputAgentOptions[D, O: BaseModel](OutputAgentOptions[O], total=False):
    tools: Required[Sequence[AppTool[D] | ToolServer]]


class _Options[D](AgentOptions, total=False):
    tools: Sequence[AppTool[D] | ToolServer]
    output: object


class Agent[D, O]:
    """spec/api.json `Agent`: reusable; every run is a separate thread unless `thread` is given.
    A completed run's output is an `O`: the final text, or the `output` model."""

    def __init__(
        self,
        definition: Definition[D],
        default_deps: tuple[D] | tuple[()],
        decode: Callable[[str], O],
    ) -> None:
        self._definition = definition
        self._default_deps = default_deps
        self._decode: Final = decode

    @property
    def name(self) -> str:
        return self._definition.name

    @property
    def definition(self) -> Definition[D]:
        """What this agent pins; a parent starts it as a subagent or handoff target."""
        return self._definition

    def _deps(self, options: RunOptions[D]) -> D:
        if "deps" in options:
            return options["deps"]
        if self._default_deps:
            return self._default_deps[0]
        tools: list[tuple[str, ContextDeps]] = [
            (t.name, _deps_of(t)) for t in self._definition.tools
        ]
        raise ConfigError("invalid_config", missing_deps(self.name, tools))

    @overload
    async def run(
        self: "Agent[None, O]", input: Input, **options: Unpack[RunOptions[None]]
    ) -> RunResult[O]: ...
    @overload
    async def run(self, input: Input, **options: Unpack[RunOptionsWithDeps[D]]) -> RunResult[O]: ...
    async def run[T](
        self: "Agent[T, O]", input: Input, **options: Unpack[RunOptions[T]]
    ) -> RunResult[O]:
        """Runs one input to a terminal result. Needs no server. `deps` is required only when
        the agent's tools read deps; tools typed `RunContext[None]` see None."""
        return await self._run(input, options, self._deps(options), _drop)

    @overload
    def run_sync(
        self: "Agent[None, O]", input: Input, **options: Unpack[RunOptions[None]]
    ) -> RunResult[O]: ...
    @overload
    def run_sync(self, input: Input, **options: Unpack[RunOptionsWithDeps[D]]) -> RunResult[O]: ...
    def run_sync[T](
        self: "Agent[T, O]", input: Input, **options: Unpack[RunOptions[T]]
    ) -> RunResult[O]:
        """`run` for scripts: runs on a new event loop in this thread. Inside a running event
        loop, await `run` instead."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._run(input, options, self._deps(options), _drop))
        why = "run_sync can't be called inside a running event loop; use await agent.run(...)"
        raise ConfigError("invalid_config", why)

    @overload
    def stream(
        self: "Agent[None, O]", input: Input, **options: Unpack[RunOptions[None]]
    ) -> RunStream[O]: ...
    @overload
    def stream(self, input: Input, **options: Unpack[RunOptionsWithDeps[D]]) -> RunStream[O]: ...
    def stream[T](
        self: "Agent[T, O]", input: Input, **options: Unpack[RunOptions[T]]
    ) -> RunStream[O]:
        """The same run as `run`, streamed. Call it inside a running event loop."""
        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        run = self._run(input, options, self._deps(options), queue.put_nowait)
        return RunStream(queue, asyncio.get_running_loop().create_task(run))

    async def _run(self, input: Input, options: RunOptions[D], deps: D, emit: Emit) -> RunResult[O]:
        # A run reports its output as text: the final text, or the accepted value as canonical
        # JSON, which parses back into the output model.
        done = await execute(self._definition, input, options, deps, emit)
        if isinstance(done, Completed):
            return Completed(self._decode(done.output), done.thread)
        return done

    async def check(self) -> Ok[None] | Err[Failure]:
        """spec/api.json `Agent.check`: resolves setup (credentials, MCP handshakes) without
        starting a run. Every setup failure comes back as a value, never raised; fix the
        environment and check again on the same agent. MCP sessions it opens are closed before
        it returns."""
        try:
            await set_up(self._definition)
            async with AsyncExitStack() as sessions:
                pinned = await with_servers(self._definition, sessions, outside_any_branch)
                pinned.pin()
        except ConfigError as error:
            return Err(Failure(error.code, error.message))
        return Ok(None)


def _drop(_item: StreamEvent) -> None:
    pass


def _text(text: str) -> str:
    return text


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
    output = _output(options.get("output"))
    decode: Callable[[str], object] = _text
    if output is not None:
        decode = partial(output.model_validate_json, strict=True)
    given = options.get("tools", ())
    servers = tuple(t for t in given if isinstance(t, ToolServer))
    tools = tuple(t for t in given if not isinstance(t, ToolServer))
    if _take_no_deps(tools):
        return Agent(_definition(options, tools, servers, output), (None,), decode)
    return Agent(_definition(options, tools, servers, output), (), decode)


def _take_no_deps[D](tools: tuple[AppTool[D], ...]) -> TypeGuard[tuple[AppTool[None], ...]]:
    """Every tool's context is annotated `RunContext[None]` (or there are none), so a run's deps
    default to None. Read from the annotations, which are the tools' static type."""
    return all(_deps_of(t) == "none" for t in tools)


def _deps_of(app_tool: object) -> ContextDeps:
    """A `tool()`'s context annotation; another AppTool's is never read, so it needs deps."""
    # Read before narrowing: only the callable matters, not the Tool's type parameters.
    execute: object = getattr(app_tool, "execute", None)
    return context_deps(execute) if isinstance(app_tool, Tool) else "deps"


def _output(output: object) -> type[BaseModel] | None:
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


def _retries(options: AgentOptions) -> int:
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


def _definition[T](
    options: AgentOptions,
    tools: tuple[AppTool[T], ...],
    servers: tuple[ToolServer, ...],
    output: type[BaseModel] | None,
) -> Definition[T]:
    sandbox = options.get("sandbox")
    egress = options.get("egress", ())
    if egress != "unenforced" and egress:
        raise ConfigError("egress_policy_unsupported", "egress host allowlists aren't supported")
    if sandbox is not None and sandbox.info.egress == "unenforced" and egress != "unenforced":
        # A provider that can't enforce egress needs an explicit opt-in.
        raise ConfigError("egress_policy_unsupported", f"{sandbox.info.provider}: egress")
    definition = Definition(
        options.get("name", "agent"),
        options["model"],
        options.get("instructions", ""),
        tools,
        options.get("permissions"),
        options.get("budget"),
        options.get("retry"),
        options.get("context"),
        sandbox,
        egress,
        tuple(options.get("extensions", ())),
        options.get("memory"),
        options.get("memory_write", "ask"),
        options.get("knowledge"),
        servers,
        tuple(replace(a.definition, member=True) for a in options.get("subagents", ())),
        tuple(a.definition for a in options.get("handoffs", ())),
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
    )
    if "approvers" in options:
        definition = replace(definition, approvers=tuple(options["approvers"]))
    if "on_unknown_usage" in options:
        definition = replace(definition, on_unknown_usage=options["on_unknown_usage"])
    pinned = frozenset(s.name for s in definition.specs())
    children = tuple(replace(c, allowed=pinned) for c in definition.subagents)
    definition = replace(definition, subagents=children)
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
