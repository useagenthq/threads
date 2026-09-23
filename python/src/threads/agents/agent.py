"""`agent()` (spec/api.json): a pure definition, run with `run()` or `stream()`.

`agent()` does no I/O, reads no env and opens no sockets; a run binds the
store, the principal and the deps.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from typing import Literal, Required, TypedDict, Unpack, overload

from threads.agents import narrowing
from threads.agents.bindings import AppTool, ToolServer
from threads.agents.builtins import Egress
from threads.agents.catalog import GitOptions, LspOptions, WebOptions, catalog
from threads.agents.config import ConfigError
from threads.agents.definition import Definition
from threads.agents.results import RunResult, StreamEvent
from threads.agents.run import Input, RunOptions, execute
from threads.hooks.extension import Extension
from threads.log import Budget, Context, Permissions, Principal, Retry
from threads.loop.model import Model
from threads.memory.authority import MemoryWrite
from threads.memory.protocol import KnowledgeProvider, MemoryProvider
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
    subagents: "Sequence[Agent[None]]"
    """Agents spawn_agent may start, by name; the team tools come with them."""
    handoffs: "Sequence[Agent[None]]"
    """Agents this one may hand the conversation to, pinned as policy.handoffs."""
    web: WebOptions
    """Host-side web_fetch and web_search."""
    git: GitOptions
    """The git gateway tools, with the forge credential kept on the host."""
    computer: bool
    """computer_screenshot and computer; needs a sandbox with a desktop."""
    lsp: LspOptions
    """lsp for these languages, served by the sandbox image."""
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


class _Options[D](AgentOptions, total=False):
    tools: Sequence[AppTool[D] | ToolServer]


class RunStream:
    """spec/api.json `RunStream`: the run's committed events as they are appended, then its
    result. A subscription to the log, not a second loop."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        self._task: asyncio.Task[RunResult[str]] | None = None

    def attach(self, task: asyncio.Task[RunResult[str]]) -> None:
        """Binds the run this stream reports."""
        self._task = task
        task.add_done_callback(lambda _: self._queue.put_nowait(None))

    def emit(self, item: StreamEvent) -> None:
        self._queue.put_nowait(item)

    @property
    def result(self) -> asyncio.Task[RunResult[str]]:
        """Await it for the `RunResult`."""
        if self._task is None:
            raise AssertionError("a stream starts its run when it is made")
        return self._task

    def __aiter__(self) -> AsyncIterator[StreamEvent]:
        return self._items()

    async def _items(self) -> AsyncIterator[StreamEvent]:
        while (item := await self._queue.get()) is not None:
            yield item


class Agent[D]:
    """spec/api.json `Agent`: reusable; every run is a separate thread unless `thread` is given."""

    def __init__(self, definition: Definition[D], default_deps: tuple[D] | tuple[()]) -> None:
        self._definition = definition
        self._default_deps = default_deps

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
        raise ConfigError("invalid_config", f"agent {self.name} needs deps for its tools")

    async def run(self, input: Input, **options: Unpack[RunOptions[D]]) -> RunResult[str]:
        """Runs one input to a terminal result. Needs no server."""
        return await execute(self._definition, input, options, self._deps(options), _drop)

    def stream(self, input: Input, **options: Unpack[RunOptions[D]]) -> RunStream:
        """The same run as `run`, streamed. Call it inside a running event loop."""
        stream = RunStream()
        deps = self._deps(options)
        run = execute(self._definition, input, options, deps, stream.emit)
        stream.attach(asyncio.get_running_loop().create_task(run))
        return stream


def _drop(_item: StreamEvent) -> None:
    pass


@overload
def agent(**options: Unpack[AgentOptions]) -> Agent[None]: ...
@overload
def agent(**options: Unpack[ServerAgentOptions]) -> Agent[None]: ...
@overload
def agent[D](**options: Unpack[ToolAgentOptions[D]]) -> Agent[D]: ...
def agent[D](**options: Unpack[_Options[D]]) -> Agent[D] | Agent[None]:
    """spec/api.json `agent`. Pure: no I/O. Raises ConfigError for duplicate tool names."""
    given = options.get("tools", ())
    servers = tuple(t for t in given if isinstance(t, ToolServer))
    tools = tuple(t for t in given if not isinstance(t, ToolServer))
    if not tools:
        empty: tuple[AppTool[None], ...] = ()
        return Agent(_definition(options, empty, servers), (None,))
    return Agent(_definition(options, tools, servers), ())


def _definition[T](
    options: AgentOptions, tools: tuple[AppTool[T], ...], servers: tuple[ToolServer, ...]
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
        catalog=catalog(
            sandbox,
            web=options.get("web"),
            git=options.get("git"),
            computer=options.get("computer", False),
            lsp=options.get("lsp"),
        ),
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
