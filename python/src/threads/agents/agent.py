"""`agent()` (spec/api.json): a pure definition, run with `run()` or `stream()`.

`agent()` does no I/O, reads no env and opens no sockets; a run binds the
store, the principal and the deps.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Required, TypedDict, Unpack, overload

from threads.agents.bindings import AppTool
from threads.agents.builtins import Egress
from threads.agents.config import ConfigError
from threads.agents.definition import Definition
from threads.agents.results import RunResult, StreamEvent
from threads.agents.run import Input, RunOptions, execute
from threads.hooks.extension import Extension
from threads.log import Budget, Permissions, Retry
from threads.loop.model import Model
from threads.sandbox.protocol import Sandbox


class AgentOptions(TypedDict, total=False):
    model: Required[Model]
    instructions: str
    name: str
    permissions: Permissions
    budget: Budget
    retry: Retry
    sandbox: Sandbox
    """Absent: no sandbox tools. Present: bash, read, write, edit, ls, glob and grep."""
    egress: Egress
    """Sandbox egress allowlist; [] (the default) is deny-all."""
    extensions: Sequence[Extension]
    """Instructions and hooks, run in this order."""


class ToolAgentOptions[D](AgentOptions, total=False):
    tools: Required[Sequence[AppTool[D]]]


class _Options[D](AgentOptions, total=False):
    tools: Sequence[AppTool[D]]


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
def agent[D](**options: Unpack[ToolAgentOptions[D]]) -> Agent[D]: ...
def agent[D](**options: Unpack[_Options[D]]) -> Agent[D] | Agent[None]:
    """spec/api.json `agent`. Pure: no I/O. Raises ConfigError for duplicate tool names."""
    tools = tuple(options.get("tools", ()))
    if not tools:
        empty: tuple[AppTool[None], ...] = ()
        return Agent(_definition(options, empty), (None,))
    return Agent(_definition(options, tools), ())


def _definition[T](options: AgentOptions, tools: tuple[AppTool[T], ...]) -> Definition[T]:
    sandbox = options.get("sandbox")
    egress = options.get("egress", ())
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
        sandbox,
        egress,
        tuple(options.get("extensions", ())),
    )
    for kind, names in (
        ("tool", [s.name for s in definition.specs()]),
        ("extension", [e.name for e in definition.extensions]),
    ):
        if len(set(names)) != len(names):
            raise ConfigError("duplicate_name", f"{kind} names repeat: {names}")
    return definition
