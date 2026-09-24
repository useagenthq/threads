"""`agent()` (spec/api.json): a pure definition, run with `run()` or `stream()`.

`agent()` does no I/O, reads no env and opens no sockets; a run binds the
store, the principal and the deps.
"""

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Final, Unpack, overload

from threads._tool_deps import ContextDeps, context_deps, missing_deps
from threads.agents.config import ConfigError, Failure
from threads.agents.definition import Definition
from threads.agents.pinned import outside_any_branch
from threads.agents.results import Completed, RunResult, StreamEvent
from threads.agents.run import (
    Emit,
    Input,
    RunOptions,
    RunOptionsWithDeps,
    execute,
)
from threads.agents.servers import with_servers
from threads.agents.setup import set_up
from threads.agents.stream import RunStream
from threads.agents.tool import Tool
from threads.result import Err, Ok


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
            (t.name, deps_of(t)) for t in self._definition.tools
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


def deps_of(app_tool: object) -> ContextDeps:
    """A `tool()`'s context annotation; another AppTool's is never read, so it needs deps."""
    # Read before narrowing: only the callable matters, not the Tool's type parameters.
    execute: object = getattr(app_tool, "execute", None)
    return context_deps(execute) if isinstance(app_tool, Tool) else "deps"
