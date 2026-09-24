"""The dry pin drift runs on (spec lane 22, B.3, tests 4a and 4d): no setup, no secret, no MCP
connection, and for an agent without MCP or setup-bearing extensions the same line 0 and
config_hash a real run pins after setup, per first-party model adapter."""

import asyncio
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager

import httpx
import pytest
from eval_kit import LOOKUP, Order, say

from threads import Agent, ConfigError, RunContext, agent, fake_sandbox, scripted_model, tool
from threads.adapters.models.anthropic import anthropic
from threads.adapters.models.litellm import litellm
from threads.adapters.models.openai import openai
from threads.agents.bindings import AppTool, Fence
from threads.agents.definition import dry_pin
from threads.agents.setup import set_up
from threads.hooks.extension import extension
from threads.log import ThreadStartedData
from threads.memory.types import MemoryRecord, ProviderError, Scope
from threads.render.request import line0
from threads.result import Err

COUNTS: dict[str, int] = {"connects": 0, "extension": 0, "memory": 0}


class _Refusing:
    """An MCP server that refuses every connection, counting the attempts (none may come)."""

    name = "jira"

    @asynccontextmanager
    async def connect(self, fence: Fence) -> AsyncGenerator[Sequence[AppTool[object]]]:
        assert fence is not None
        COUNTS["connects"] += 1
        raise ConnectionRefusedError("connection refused")
        yield ()


class _Memory:
    """A memory provider with a setup step (like mem0, Supermemory and Zep)."""

    async def setup(self) -> None:
        COUNTS["memory"] += 1

    async def remember(self, scope: Scope, record: MemoryRecord, key: str) -> Err[ProviderError]:
        return Err(ProviderError("unavailable", "not in tests"))

    async def recall(self, scope: Scope, query: str, *, k: int = 5) -> Err[ProviderError]:
        return Err(ProviderError("unavailable", "not in tests"))

    async def forget(self, scope: Scope, id: str, key: str) -> Err[ProviderError]:
        return Err(ProviderError("unavailable", "not in tests"))


async def _counted() -> None:
    COUNTS["extension"] += 1


async def _email(args: Order, _ctx: RunContext[None]) -> str:
    return args.id


CRM = extension(
    name="crm",
    instructions="Look customers up in the CRM.",
    tools=[
        tool(
            name="crm_lookup",
            description="Look a customer up.",
            input=Order,
            runs="host",
            effect="read_only",
            execute=_email,
        )
    ],
    setup=_counted,
)


def test_runs_no_setup_reads_no_secret_opens_no_mcp_and_names_what_it_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bot = agent(
        name="support",
        model=anthropic("claude-test", max_input_tokens=1000, max_output_tokens=8),
        tools=[LOOKUP, _Refusing()],
        extensions=[CRM],
        memory=_Memory(),
    )
    dry = dry_pin(bot.definition)
    assert COUNTS == {"connects": 0, "extension": 0, "memory": 0}
    assert (dry.mcp, dry.setup_extensions, dry.setup_providers) == (
        ("jira",),
        ("crm",),
        ("memory",),
    )
    tools = dry.started["tools"]
    assert isinstance(tools, list)
    crm = next(t for t in tools if isinstance(t, dict) and t.get("name") == "crm__crm_lookup")
    assert isinstance(crm, dict)
    assert crm["origin"] == {"extension": "crm"}
    # The real pin needs the key its setup resolves: the dry pin never asked for it.
    with pytest.raises(ConfigError):
        asyncio.run(set_up(bot.definition))


def _line0(started: object) -> bytes:
    return line0(ThreadStartedData.model_validate(started), None)


AUDIT = extension(
    name="audit",
    instructions="Log every refund.",
    tools=[
        tool(
            name="note",
            description="Write a note.",
            input=Order,
            runs="host",
            effect="read_only",
            execute=_email,
        )
    ],
)
ADAPTERS: dict[str, Callable[[], Agent[None, str]]] = {
    "anthropic": lambda: agent(
        name="support", model=anthropic("claude-test", max_input_tokens=1000, max_output_tokens=8)
    ),
    "openai": lambda: agent(
        name="support", model=openai("gpt-test", max_input_tokens=1000, max_output_tokens=8)
    ),
    "litellm": lambda: agent(
        name="support",
        model=litellm(
            "openai/gpt-test", max_input_tokens=1000, max_output_tokens=8, cache_ttl_ms=300_000
        ),
    ),
    "a bare agent": lambda: agent(model=scripted_model({"responses": [say("hi")]})),
    "tools, a sandbox and a fallback": lambda: agent(
        name="support",
        instructions="You handle refunds.",
        model=scripted_model({"responses": [say("hi")]}),
        fallback=[scripted_model({"responses": []})],
        tools=[LOOKUP],
        sandbox=fake_sandbox(),
    ),
    "an extension without setup": lambda: agent(
        name="support", model=scripted_model({"responses": []}), extensions=[AUDIT]
    ),
    "subagents": lambda: agent(
        name="lead",
        model=scripted_model({"responses": []}),
        subagents=[agent(name="reviewer", model=scripted_model({"responses": []}))],
    ),
}
KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")


@pytest.mark.parametrize("name", list(ADAPTERS))
def test_the_dry_pin_equals_the_real_pin(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def blocked(*_args: object, **_kwargs: object) -> httpx.Response:
        raise AssertionError("the network is blocked in this test")

    monkeypatch.setattr(httpx.AsyncClient, "send", blocked)
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    keyless = dry_pin(ADAPTERS[name]().definition).started
    for key in KEYS:
        monkeypatch.setenv(key, "sk-fake-for-the-dry-pin-test")
    bot = ADAPTERS[name]()
    dry = dry_pin(bot.definition).started
    asyncio.run(set_up(bot.definition))
    real = bot.definition.pin()[0]
    assert _line0(keyless) == _line0(dry) == _line0(real)
    assert dry["config_hash"] == real["config_hash"] == keyless["config_hash"]
