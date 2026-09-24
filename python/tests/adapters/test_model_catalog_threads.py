"""Lane 06 through the agent: an OpenAI output-token budget is enforceable with the catalog's
default cap, explicit limits equal to the catalog's continue a thread (what a withdrawal's message
prints), and a thread started before the catalog can't continue but stays readable."""

import asyncio
import dataclasses
from typing import Unpack

import pytest
from kit import text

from threads import Completed, Store, agent, sqlite
from threads.adapters.models.openai.model import OpenAIOptions
from threads.agents.config import ConfigError
from threads.log import Budget, ThreadId
from threads.loop.model import ModelInfo
from threads.loop.scripted import ScriptedModel, scripted_model
from threads.openai import openai
from threads.result import Ok
from threads.thread.handle import Thread, open_thread


def gpt(info: ModelInfo | None = None, **options: Unpack[OpenAIOptions]) -> ScriptedModel:
    """A scripted model declaring what openai("gpt-5.5", **options) declares (or `info`): the
    pins are the adapter's, the transport is the test's."""
    model = scripted_model({"responses": [text("ok")]})
    model._info = info or openai("gpt-5.5", **options).info  # pyright: ignore[reportPrivateUsage] - a test adapter
    return model


def test_an_output_token_budget_is_enforceable_with_the_default_cap() -> None:
    bot = agent(model=gpt(), budget=Budget(max_output_tokens=50_000))
    assert asyncio.run(bot.check()) == Ok(None)


def test_explicit_limits_equal_to_the_catalog_s_continue_a_thread() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        first = await agent(model=gpt()).run("hi", store=store)
        assert isinstance(first, Completed)
        corrected = agent(model=gpt(max_input_tokens=900_000))
        with pytest.raises(ConfigError) as refused:
            await corrected.run("again", thread=first.thread)
        assert refused.value.code == "invalid_config"
        original: OpenAIOptions = {"max_input_tokens": 922_000, "max_output_tokens": 128_000}
        second = await agent(model=gpt(**original)).run("again", thread=first.thread)
        assert isinstance(second, Completed)

    asyncio.run(main())


def test_a_thread_started_before_the_catalog_can_t_continue_and_stays_readable() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        now = openai("gpt-5.5").info
        # The factory before lane 06 pinned the model's whole output cap as max_output_tokens.
        before = dataclasses.replace(now, params={"max_output_tokens": 128_000})
        first = await agent(model=gpt(before)).run("hi", store=store)
        assert isinstance(first, Completed)
        count = await entries(await thread(store, first.thread.id))
        with pytest.raises(ConfigError) as refused:
            await agent(model=gpt()).run("again", thread=first.thread)
        assert refused.value.code == "invalid_config"
        after = await thread(store, first.thread.id)
        assert await entries(after) == count
        assert isinstance(await after.cost(), Ok)

    asyncio.run(main())


async def thread(store: Store, thread_id: ThreadId) -> Thread:
    opened = await open_thread(store, thread_id)
    assert isinstance(opened, Ok)
    return opened.value


async def entries(handle: Thread) -> int:
    timeline = await handle.timeline()
    assert isinstance(timeline, Ok)
    return len(timeline.value.entries)
