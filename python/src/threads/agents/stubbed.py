"""A run in stub mode: every mediated operation (host
and MCP tools, channel sends, memory and knowledge writes, the web and git gateways) is answered
by the stub gateway and never goes live. The sandbox's own tools run for real only in a sandbox
whose egress is deny-all, so nothing they do leaves it; otherwise they are stubbed too and an
unrecorded call fails closed. Reading a spilled result stays a host read of this log.

A live model that declares hosted tools is refused before recovery or any dispatch: hosted calls
run inside the provider and can't be stubbed.
"""

from threads.agents.config import ConfigError
from threads.log import JsonObject, ToolSpec
from threads.loop.model import LookupResult, ModelInfo
from threads.loop.stubs import Stub, StubGateway
from threads.loop.tools import Dispatched, Invocation, Termination, ToolRunner
from threads.tools import HOST, SANDBOX_TOOLS

TEST_KIT = "scripted"
"""The adapter of the scripted test model: never live, so its declarations are data."""


class Stubbed:
    """Routes sandbox built-ins and host reads to the run's runner, everything else to stubs."""

    def __init__(self, inner: ToolRunner, stubs: tuple[Stub, ...], *, sealed: bool) -> None:
        self._inner = inner
        self._stubs = StubGateway(stubs)
        self._sealed = sealed

    def _for(self, name: str) -> ToolRunner:
        live = name in HOST or (self._sealed and name in SANDBOX_TOOLS)
        return self._inner if live else self._stubs

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        return self._inner.invalid(spec, input)

    async def dispatch(self, call: Invocation) -> Dispatched:
        # A read_only call changes nothing anywhere: it runs for real, as in TypeScript.
        if call.spec.effect_class == "read_only":
            return await self._inner.dispatch(call)
        return await self._for(call.spec.name).dispatch(call)

    async def lookup(self, call: Invocation) -> LookupResult[str]:
        return await self._for(call.spec.name).lookup(call)

    async def terminate(self, call: Invocation) -> Termination:
        return await self._for(call.spec.name).terminate(call)

    def provider_now(self) -> int | None:
        return None


def stub_mode(
    tools: ToolRunner, stubs: tuple[Stub, ...] | None, model: ModelInfo, *, sealed: bool
) -> ToolRunner:
    """The run's tool runner: as given when live; stubbed in stub mode, where a live model with
    hosted tools raises ConfigError hosted_tool_unsupported. `sealed`: the run's sandbox denies
    all egress, so its own tools may run for real."""
    if stubs is None:
        return tools
    if model.hosted_tools and model.adapter.name != TEST_KIT:
        raise ConfigError(
            "hosted_tool_unsupported",
            f"stub mode can't stub the hosted tools of {model.model.name}: "
            f"{', '.join(model.hosted_tools)}",
        )
    return Stubbed(tools, stubs, sealed=sealed)
