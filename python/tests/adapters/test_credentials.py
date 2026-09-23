"""Lane 09: every single-account adapter takes its credential as an option defaulting to
secret("<ENV>"), resolved at setup (check() or the first run), never by the factory. A missing
one is missing_secret naming the option and the variable; a fixed environment is picked up by
the same agent; the value, given or defaulted, is redacted from tool results and never stored.

The adapter under test sits in a handoff target: the parent's setup covers it, and nothing ever
runs it, so no test reaches a provider."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel, JsonValue

from threads import (
    Agent,
    Completed,
    ConfigError,
    Failure,
    RunContext,
    agent,
    scripted_model,
    sqlite,
    tool,
)
from threads.adapters.memories.supermemory import supermemory
from threads.adapters.memories.zep import zep
from threads.adapters.models.anthropic import anthropic
from threads.adapters.models.litellm import litellm
from threads.adapters.models.openai import openai
from threads.adapters.sandboxes.daytona.sandbox import daytona
from threads.adapters.sandboxes.e2b.sandbox import e2b
from threads.adapters.sandboxes.modal.sandbox import modal
from threads.log import Permissions, ToolResultEvent
from threads.result import Err, Ok

USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}
SAY: JsonValue = {
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": USAGE,
}
ECHO_CALL: JsonValue = {
    "content": [{"type": "tool_use", "call_id": "c1", "name": "echo", "input": {}}],
    "stop_reason": "tool_use",
    "usage": USAGE,
}
ALLOW = Permissions(
    mode="default",
    allow=["echo"],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=False,
    plan_exit_mode="default",
)


def _anthropic(key: str | None) -> Agent[None]:
    model = (
        anthropic("claude-test", context_window=1000, max_output_tokens=8)
        if key is None
        else anthropic("claude-test", context_window=1000, max_output_tokens=8, api_key=key)
    )
    return agent(name="spare", model=model)


def _openai(key: str | None) -> Agent[None]:
    model = (
        openai("gpt-test", context_window=1000, max_output_tokens=8)
        if key is None
        else openai("gpt-test", context_window=1000, max_output_tokens=8, api_key=key)
    )
    return agent(name="spare", model=model)


def _litellm(key: str | None) -> Agent[None]:
    model = (
        litellm("openai/gpt-test", context_window=1000, max_output_tokens=8)
        if key is None
        else litellm("openai/gpt-test", context_window=1000, max_output_tokens=8, api_key=key)
    )
    return agent(name="spare", model=model)


def _e2b(key: str | None) -> Agent[None]:
    return agent(name="spare", model=scripted_model({"responses": []}), sandbox=e2b(api_key=key))


def _daytona(key: str | None) -> Agent[None]:
    box = daytona(api_key=key)
    return agent(name="spare", model=scripted_model({"responses": []}), sandbox=box)


def _modal(key: str | None) -> Agent[None]:
    box = modal(image_id="im-x", token_id=key, token_secret=None if key is None else f"{key}-s")
    return agent(name="spare", model=scripted_model({"responses": []}), sandbox=box)


def _supermemory(key: str | None) -> Agent[None]:
    memory = supermemory(api_key=key)
    return agent(name="spare", model=scripted_model({"responses": []}), memory=memory)


def _zep(key: str | None) -> Agent[None]:
    return agent(name="spare", model=scripted_model({"responses": []}), memory=zep(api_key=key))


@dataclass(frozen=True, slots=True)
class Case:
    factory: str
    envs: tuple[str, ...]
    message: str
    label: str
    """What a given key is redacted to."""
    spare: Callable[[str | None], Agent[None]]
    """A handoff target holding the adapter, built with this key (None: the default)."""


def _one(factory: str, env: str, spare: Callable[[str | None], Agent[None]]) -> Case:
    return Case(factory, (env,), f"{factory}: set api_key or {env}", f"{factory}.api_key", spare)


CASES = (
    _one("anthropic", "ANTHROPIC_API_KEY", _anthropic),
    _one("openai", "OPENAI_API_KEY", _openai),
    _one("litellm", "OPENAI_API_KEY", _litellm),
    _one("e2b", "E2B_API_KEY", _e2b),
    _one("daytona", "DAYTONA_API_KEY", _daytona),
    Case(
        "modal",
        ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"),
        "modal: set token_id/token_secret or MODAL_TOKEN_ID/MODAL_TOKEN_SECRET",
        "modal.token_id",
        _modal,
    ),
    _one("supermemory", "SUPERMEMORY_API_KEY", _supermemory),
    _one("zep", "ZEP_API_KEY", _zep),
)
IDS = [c.factory for c in CASES]


class NoInput(BaseModel):
    pass


def _parent(spare: Agent[None], echoed: str = "") -> Agent[None]:
    async def echo(_args: NoInput, _ctx: RunContext[None]) -> str:
        return f"value={echoed}"

    echo_tool = tool(name="echo", description="Echo.", input=NoInput, execute=echo)
    script: JsonValue = {"responses": [ECHO_CALL, SAY]}
    return agent(
        model=scripted_model(script), tools=[echo_tool], permissions=ALLOW, handoffs=[spare]
    )


def _unset(monkeypatch: pytest.MonkeyPatch, case: Case) -> None:
    for env in case.envs:
        monkeypatch.delenv(env, raising=False)


def _set(monkeypatch: pytest.MonkeyPatch, case: Case, value: str) -> None:
    for env in case.envs:
        monkeypatch.setenv(env, value)


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_a_missing_key_fails_check_as_a_value_and_the_run_before_any_append(
    case: Case, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _unset(monkeypatch, case)
    bot = _parent(case.spare(None))  # the factory and agent() read no env

    async def main() -> None:
        assert await bot.check() == Err(Failure("missing_secret", case.message))
        with pytest.raises(ConfigError) as raised:
            await bot.run("go", store=sqlite(str(tmp_path / "store")), deps=None)
        assert (raised.value.code, raised.value.message) == ("missing_secret", case.message)

    asyncio.run(main())
    assert not (tmp_path / "store").exists()


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_an_empty_value_counts_as_unset(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, case, "")
    checked = asyncio.run(_parent(case.spare(None)).check())
    assert checked == Err(Failure("missing_secret", case.message))


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_a_fixed_environment_is_picked_up_by_the_same_agent(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unset(monkeypatch, case)
    bot = _parent(case.spare(None))

    async def main() -> None:
        assert isinstance(await bot.check(), Err)
        _set(monkeypatch, case, f"{case.factory}-retry-key")
        assert await bot.check() == Ok(None)
        assert isinstance(await bot.run("go", store=sqlite(":memory:"), deps=None), Completed)

    asyncio.run(main())


async def _echoed(bot: Agent[None]) -> tuple[str, str]:
    """The recorded tool result's text, and every stored event as JSON."""
    result = await bot.run("go", store=sqlite(":memory:"), deps=None)
    timeline = await result.thread.timeline()
    assert isinstance(timeline, Ok)
    events = [e.event for e in timeline.value.entries]
    (echoed,) = [e for e in events if isinstance(e, ToolResultEvent)]
    return echoed.data.preview, "\n".join(e.model_dump_json() for e in events)


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_a_given_and_a_defaulted_key_are_redacted_and_never_stored(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    given = f"{case.factory}-given-key-7f3a"
    preview, stored = asyncio.run(_echoed(_parent(case.spare(given), given)))
    assert preview == f"value=[secret {case.label}]"
    assert given not in stored

    defaulted = f"{case.factory}-env-key-9b1c"
    _set(monkeypatch, case, defaulted)
    preview, stored = asyncio.run(_echoed(_parent(case.spare(None), defaulted)))
    assert preview == f"value=[secret {case.label}]"
    assert defaulted not in stored
