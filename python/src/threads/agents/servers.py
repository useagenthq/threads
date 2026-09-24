"""A run's tool servers (MCP): their sessions, opened for the run and fenced by its writer."""

import contextlib
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import replace

from threads.agents.bindings import AppTool, Fence, ToolServer
from threads.agents.config import ConfigError
from threads.agents.definition import Definition
from threads.agents.setup import redacted_error
from threads.result import Ok
from threads.store import Writer


def fenced(writer: Writer) -> Fence:
    """Whether this run still owns its branch, for a tool or provider transport's send point."""

    async def fence() -> bool:
        return isinstance(await writer.fence(), Ok)

    return fence


@asynccontextmanager
async def _closed_quietly[T](session: AbstractAsyncContextManager[T]) -> AsyncGenerator[T]:
    """`session`, closed best effort: a failed close never replaces the error, or the result, a
    run or check ends with (and its message could hold a secret)."""
    stack = AsyncExitStack()
    entered = await stack.enter_async_context(session)
    try:
        yield entered
    finally:
        with contextlib.suppress(Exception):
            await stack.aclose()


async def with_servers[D](
    definition: Definition[D], stack: AsyncExitStack, fence: Fence
) -> Definition[D]:
    """The definition with its tool servers' tools, on sessions `stack` closes: app tools in
    declared order, then server tools sorted by name. A run fences them by its writer; check()
    lists them outside any branch. A server tool named like another tool is duplicate_name."""
    if not definition.servers:
        return definition

    found: list[AppTool[object]] = []
    for server in definition.servers:
        try:
            found.extend(await stack.enter_async_context(_closed_quietly(server.connect(fence))))
        except Exception as error:
            # check() returns it and a run raises it: redacted, whatever it was (C5).
            raise redacted_error(error, "mcp_unreachable", f"MCP server {server.name}") from None
    extra = sorted(found, key=lambda t: t.name)
    connected = replace(definition, tools=(*definition.tools, *extra))
    names = [s.name for s in connected.specs()]
    if len(set(names)) != len(names):
        raise ConfigError("duplicate_name", f"tool names repeat: {names}")
    return connected


def host_serving[T](definition: Definition[T], servers: tuple[ToolServer, ...]) -> Definition[T]:
    """A host's servers (a channel's send tool) go with the conversation: a handoff target
    pins them too, so the host can run it on when the conversation moves there."""
    handoffs = tuple(host_serving(h, servers) for h in definition.handoffs)
    return replace(definition, servers=(*definition.servers, *servers), handoffs=handoffs)
