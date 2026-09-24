"""The default context.cache_ttl_ms from the models' declared cache lifetimes (lane 10): agreeing
TTLs are pinned, "none" is ignored, and an unknown or disagreeing lifetime needs
context.cache_ttl_ms, checked at check() and the first run while agent() stays pure."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from typing import Literal, override

import pytest
from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads import Completed, ConfigError, Thread, agent, scripted_model, sqlite
from threads.adapters.models.litellm import litellm
from threads.log import ModelRef, ThreadStartedEvent
from threads.loop.defaults import CONTEXT
from threads.loop.model import Cache, Model, ModelChunk, ModelContext, ModelInfo, ModelRequest
from threads.loop.scripted import SCRIPTED_INFO, ScriptedModel
from threads.result import Err, Ok

SAID: JsonValue = {
    "content": [{"type": "text", "text": "Hi."}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 2},
}
HOUR_MS, DAY_MS, TEN_MINUTES_MS, FIVE_MS = 3_600_000, 86_400_000, 600_000, 300_000
HOUR: Cache = {"ttl_ms": HOUR_MS}
FIVE: Cache = {"ttl_ms": 300_000}
DAY: Cache = {"ttl_ms": DAY_MS}


class Declaring(ScriptedModel):
    """A scripted model under another name, declaring another cache lifetime."""

    def __init__(self, info: ModelInfo) -> None:
        super().__init__([], {})
        self.inner = scripted_model({"responses": [SAID]})
        self._declared = info

    @property
    @override
    def info(self) -> ModelInfo:
        return self._declared

    @override
    def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        return self.inner.send(request, context)


def declaring(name: str, cache: Cache | None) -> Declaring:
    """A scripted model that declares `cache` like the named adapter model would."""
    provider, model = name.split("/")
    ref = ModelRef(provider=provider, name=model)
    limits = SCRIPTED_INFO.limits.model_copy(update={"provider": provider, "name": model})
    info = replace(SCRIPTED_INFO, model=ref, limits=limits, cache=cache)
    return Declaring(info)


async def _started(thread: Thread) -> ThreadStartedEvent:
    timeline = await thread.timeline()
    assert isinstance(timeline, Ok)
    return next(e.event for e in timeline.value.entries if isinstance(e.event, ThreadStartedEvent))


def pinned(primary: Model, fallback: Sequence[Model] = (), ttl: int | None = None) -> int:
    """The pinned policy.context.cache_ttl_ms: every pin holds a complete context section."""

    async def main() -> int:
        bot = agent(model=primary, fallback=list(fallback))
        if ttl is not None:
            context = CONTEXT.model_copy(update={"cache_ttl_ms": ttl})
            bot = agent(model=primary, fallback=list(fallback), context=context)
        done = await bot.run("hi", store=sqlite(":memory:"))
        assert isinstance(done, Completed)
        policy = (await _started(done.thread)).data.policy
        assert policy is not MISSING
        assert policy.context is not MISSING
        return policy.context.cache_ttl_ms

    return asyncio.run(main())


def refused(primary: Model, fallback: Sequence[Model] = ()) -> str:
    # agent() is pure: the check runs at check() (and the first run).
    checked = asyncio.run(agent(model=primary, fallback=list(fallback)).check())
    assert isinstance(checked, Err)
    assert checked.error.code == "invalid_config"
    return checked.error.message


def test_1h_primary_and_1h_fallback_pin_3600000() -> None:
    a, b = declaring("anthropic/claude-sonnet-5", HOUR), declaring("anthropic/haiku", HOUR)
    assert pinned(a, [b]) == HOUR_MS


def test_1h_primary_and_5m_fallback_need_context_cache_ttl_ms() -> None:
    a, b = declaring("anthropic/claude-sonnet-5", HOUR), declaring("anthropic/haiku", FIVE)
    assert refused(a, [b]) == (
        "anthropic/claude-sonnet-5 caches for 1h but anthropic/haiku for 5m: "
        "set cache_ttl_ms in the agent's context"
    )
    a, b = declaring("anthropic/claude-sonnet-5", HOUR), declaring("anthropic/haiku", FIVE)
    assert pinned(a, [b], ttl=HOUR_MS) == HOUR_MS


def test_1h_primary_and_an_openai_fallback_are_refused() -> None:
    a, b = declaring("anthropic/claude-sonnet-5", HOUR), declaring("openai/gpt-5.5", FIVE)
    assert "but openai/gpt-5.5 for 5m" in refused(a, [b])


def test_openai_models_with_24h_retention_pin_86400000() -> None:
    a, b = declaring("openai/gpt-5.5", DAY), declaring("openai/gpt-5.5-mini", DAY)
    assert pinned(a, [b]) == DAY_MS


def test_5m_everywhere_pins_the_default_300000() -> None:
    a, b = declaring("anthropic/claude-sonnet-5", FIVE), declaring("openai/gpt-5.5", FIVE)
    assert pinned(a, [b]) == FIVE_MS


def test_a_fallback_that_never_caches_is_ignored() -> None:
    a, b = declaring("anthropic/claude-sonnet-5", HOUR), declaring("anthropic/haiku", "none")
    assert pinned(a, [b]) == HOUR_MS


def test_an_unknown_lifetime_is_refused_even_beside_a_5m_primary() -> None:
    why = (
        "the cache lifetime of litellm/large is unknown: set cache_ttl_ms in the agent's "
        "context, or declare info.cache on the model (litellm() takes it as cache_ttl_ms)"
    )
    bridge = declaring("litellm/large", None)
    assert refused(declaring("anthropic/claude-sonnet-5", HOUR), [bridge]) == why
    assert refused(declaring("anthropic/haiku", FIVE), [bridge]) == why
    declared = declaring("litellm/large", FIVE)
    assert pinned(declaring("anthropic/haiku", FIVE), [declared]) == FIVE_MS


def test_a_model_alone_with_an_unknown_lifetime_needs_it_declared() -> None:
    assert "is unknown" in refused(declaring("litellm/large", None))
    assert pinned(declaring("litellm/large", None), ttl=TEN_MINUTES_MS) == TEN_MINUTES_MS
    assert pinned(declaring("litellm/large", "none")) == FIVE_MS


def test_agent_stays_pure() -> None:
    a, b = declaring("anthropic/claude-sonnet-5", HOUR), declaring("anthropic/haiku", FIVE)
    agent(model=a, fallback=[b])


def _litellm(cache_ttl_ms: int | Literal["none"] | None = None) -> Model:
    if cache_ttl_ms is None:
        return litellm("openai/m", max_input_tokens=1000, max_output_tokens=100)
    return litellm(
        "openai/m", max_input_tokens=1000, max_output_tokens=100, cache_ttl_ms=cache_ttl_ms
    )


def test_litellm_declares_its_cache_lifetime_only_when_given() -> None:
    assert _litellm().info.cache is None
    assert _litellm(cache_ttl_ms=300_000).info.cache == {"ttl_ms": 300_000}
    assert _litellm(cache_ttl_ms="none").info.cache == "none"


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "5m"])
def test_litellm_refuses_a_cache_ttl_that_is_not_a_positive_integer(bad: object) -> None:
    with pytest.raises(ConfigError) as raised:
        _litellm(cache_ttl_ms=bad)  # type: ignore[arg-type] - an untyped caller
    assert raised.value.code == "invalid_config"


def test_the_scripted_model_never_caches() -> None:
    assert SCRIPTED_INFO.cache == "none"


def test_a_partial_context_without_cache_ttl_takes_the_models_lifetime() -> None:
    """As in TypeScript: only a cache_ttl_ms the agent names skips the models' lifetime."""
    a, b = declaring("anthropic/claude-sonnet-5", HOUR), declaring("anthropic/haiku", HOUR)

    async def main() -> tuple[int, int]:
        bot = agent(model=a, fallback=[b], context={"reserve_tokens": 1234})
        done = await bot.run("hi", store=sqlite(":memory:"))
        assert isinstance(done, Completed)
        policy = (await _started(done.thread)).data.policy
        assert policy is not MISSING
        assert policy.context is not MISSING
        return policy.context.cache_ttl_ms, policy.context.reserve_tokens

    assert asyncio.run(main()) == (HOUR_MS, 1234)
    bridge = declaring("litellm/large", None)
    checked = asyncio.run(agent(model=bridge, context={"reserve_tokens": 1234}).check())
    assert isinstance(checked, Err)
    assert "is unknown" in checked.error.message
    named = agent(model=bridge, context={"cache_ttl_ms": TEN_MINUTES_MS})
    assert isinstance(asyncio.run(named.check()), Ok)
