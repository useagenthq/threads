"""`mcp()` (spec/api.json, ): an MCP server in one line, on the official
MCP Python SDK. The host owns the connection and the credentials: `secret()` values are resolved
here, sent only in the host's own requests or the host-spawned process, and never reach the log,
a prompt or the sandbox.

At run setup the server is connected, its tools are listed, checked (a real JSON Schema
validator, never core), filtered and pinned sorted as `mcp__<server>__<tool>`. An unreachable
server or a failed handshake is `ConfigError("mcp_unreachable")` naming the server: the agent
never starts with its tools silently missing.
"""

import asyncio
import os
import re
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Final, Literal, Required, TypedDict, Unpack

import httpx
from jsonschema import Draft202012Validator, SchemaError
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import PaginatedRequestParams, Tool
from pydantic import JsonValue, TypeAdapter, ValidationError

from threads.adapters.mcp.tool import READ_RESOURCE, READ_RESOURCE_SCHEMA, McpTool
from threads.adapters.mcp.transport import Fence, FencedTransport, Streams, stdio
from threads.agents.config import ConfigError
from threads.log import EffectClass
from threads.secrets import Secret, resolve

SETUP_S: Final = 30.0
_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SCHEMA: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])

type Value = str | Secret


class ToolFilter(TypedDict, total=False):
    allow: Sequence[str]
    deny: Sequence[str]


class McpOptions(TypedDict, total=False):
    name: Required[str]
    url: str
    """Streamable HTTP. Exactly one of url or command."""
    headers: Mapping[str, Value]
    command: str
    """stdio, spawned on the host."""
    args: Sequence[str]
    env: Mapping[str, Value]
    runs: Literal["host", "sandbox"]
    tools: ToolFilter
    effect: EffectClass
    """Undeclared: unguarded, so an uncertain call parks and is never retried (F1.10)."""


@dataclass(frozen=True, slots=True)
class McpServer:
    """spec/api.json `McpServer`. Build it with `mcp()`."""

    name: str
    url: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    headers: Mapping[str, Value] = field(default_factory=dict[str, Value])
    env: Mapping[str, Value] = field(default_factory=dict[str, Value])
    allow: frozenset[str] | None = None
    deny: frozenset[str] = frozenset()
    effect: EffectClass = "unguarded"
    http: httpx.AsyncBaseTransport | None = None
    """The HTTP transport under the fence; None opens real connections. Tests pass one."""

    def connect(self, fence: Fence) -> AbstractAsyncContextManager[Sequence[McpTool]]:
        """The server's pinned tools for one run, live until the context exits."""
        return self._connect(fence)

    @asynccontextmanager
    async def _connect(self, fence: Fence) -> AsyncGenerator[Sequence[McpTool]]:
        headers = {k: _value(v) for k, v in self.headers.items()}
        env = {k: _value(v) for k, v in self.env.items()}
        ready: asyncio.Future[tuple[McpTool, ...]] = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()

        async def own() -> None:
            # The SDK's task groups live in this task, so a dying server can never cancel the
            # run itself; the run's calls then fail as connection closed (uncertain).
            try:
                async with (
                    self._streams(fence, headers, env) as streams,
                    ClientSession(*streams) as session,
                ):
                    init = await session.initialize()
                    offers = init.capabilities.resources is not None
                    ready.set_result(self._pin(session, await _listed(session), resources=offers))
                    await stop.wait()
            except Exception as error:
                if not ready.done():
                    ready.set_exception(error)

        owner = asyncio.create_task(own())
        try:
            async with asyncio.timeout(SETUP_S):
                tools = await asyncio.shield(ready)
        except Exception as error:
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
            if isinstance(error, ConfigError):
                raise
            raise ConfigError("mcp_unreachable", f"MCP server {self.name}: {error!r}") from error
        try:
            yield tools
        finally:
            stop.set()
            await asyncio.gather(owner, return_exceptions=True)

    def _streams(
        self, fence: Fence, headers: Mapping[str, str], env: Mapping[str, str]
    ) -> AbstractAsyncContextManager[Streams]:
        if self.command is not None:
            base = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
            return stdio(self.command, self.args, {**base, **env}, fence)
        if self.url is None:
            raise AssertionError("mcp() requires url or command")
        return _http(self.url, self.http or httpx.AsyncHTTPTransport(), fence, headers)

    def _pin(
        self, session: ClientSession, listed: Sequence[Tool], *, resources: bool
    ) -> tuple[McpTool, ...]:
        kept = [
            t
            for t in listed
            if (self.allow is None or t.name in self.allow) and t.name not in self.deny
        ]
        tools = [self._tool(session, t) for t in kept]
        if resources:
            tools.append(self._reader(session))
        names = [t.name for t in tools]
        if len(set(names)) != len(names):
            raise ConfigError("invalid_config", f"MCP server {self.name}: tool names collide")
        return tuple(sorted(tools, key=lambda t: t.name))

    def _reader(self, session: ClientSession) -> McpTool:
        name = f"mcp__{self.name}__{READ_RESOURCE}"
        description = f"Read a resource of the {self.name} MCP server by URI."
        return McpTool(
            name, READ_RESOURCE, description, READ_RESOURCE_SCHEMA, "read_only", session, True
        )

    def _tool(self, session: ClientSession, tool: Tool) -> McpTool:
        name = f"mcp__{self.name}__{re.sub(r'[^a-z0-9_]', '_', tool.name.lower())}"
        if _NAME.fullmatch(name) is None:
            raise ConfigError("invalid_config", f"MCP tool {tool.name}: {name} is not a wire Name")
        try:
            schema = _SCHEMA.validate_python(tool.inputSchema)
            Draft202012Validator.check_schema(schema)
        except (ValidationError, SchemaError) as error:
            message = f"MCP tool {tool.name}: invalid input schema: {error}"
            raise ConfigError("invalid_config", message) from error
        description = tool.description or tool.title or tool.name
        return McpTool(name, tool.name, description, schema, self.effect, session)


@asynccontextmanager
async def _http(
    url: str, inner: httpx.AsyncBaseTransport, fence: Fence, headers: Mapping[str, str]
) -> AsyncGenerator[Streams]:
    """Streamable HTTP through the SDK, on an httpx client whose transport is fenced."""
    client = httpx.AsyncClient(
        transport=FencedTransport(inner, fence),
        headers=dict(headers),
        timeout=httpx.Timeout(30.0, read=300.0),
    )
    async with client, streamable_http_client(url, http_client=client) as (read, write, _):
        yield read, write


def _value(value: Value) -> str:
    return resolve(value) if isinstance(value, Secret) else value


async def _listed(session: ClientSession) -> list[Tool]:
    tools: list[Tool] = []
    cursor: str | None = None
    while True:
        page = await session.list_tools(params=PaginatedRequestParams(cursor=cursor))
        tools.extend(page.tools)
        cursor = page.nextCursor
        if cursor is None:
            return tools


def mcp(**options: Unpack[McpOptions]) -> McpServer:
    """spec/api.json `mcp`. Pure: nothing connects until a run starts."""
    name = options["name"]
    if _NAME.fullmatch(name) is None or "__" in name:
        raise ConfigError("invalid_config", f"MCP server name {name!r} is not a wire Name")
    url, command = options.get("url"), options.get("command")
    if (url is None) == (command is None):
        raise ConfigError("invalid_config", f"MCP server {name}: give exactly one of url, command")
    if options.get("runs", "host") == "sandbox":
        # ponytail: in-sandbox stdio servers aren't built; host-spawned is the default.
        raise ConfigError("capability_missing", f"MCP server {name}: runs='sandbox'")
    if options.get("effect") == "idempotent":
        # ponytail: no dedup window option and no effect key on the call yet, so the claim
        # couldn't be honored; add both (api.json first) when a server dedups on a key.
        raise ConfigError("invalid_config", f"MCP server {name}: effect='idempotent'")
    filters = options.get("tools", {})
    allow = filters.get("allow")
    return McpServer(
        name,
        url,
        command,
        tuple(options.get("args", ())),
        dict(options.get("headers", {})),
        dict(options.get("env", {})),
        None if allow is None else frozenset(allow),
        frozenset(filters.get("deny", ())),
        options.get("effect", "unguarded"),
    )
