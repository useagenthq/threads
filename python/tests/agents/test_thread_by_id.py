"""Agent.run and Agent.stream continue a thread given by its id, as in TypeScript: its main
branch in `store` (spec/api.json Agent.run thread: ThreadId or Thread)."""

import asyncio

import pytest
from pydantic import JsonValue

from threads import Completed, ConfigError, agent, scripted_model, sqlite
from threads.log import ThreadId, UserInputEvent
from threads.result import Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def test_a_thread_id_continues_that_threads_main_branch() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(model=scripted_model({"responses": [text("one"), text("two"), text("3")]}))
        first = await bot.run("a", store=store)
        second = await bot.run("b", store=store, thread=first.thread.id)
        assert isinstance(second, Completed)
        assert (second.thread.id, second.thread.branch) == (first.thread.id, first.thread.branch)
        streamed = bot.stream("c", store=store, thread=first.thread.id)
        async for _ in streamed:
            pass
        third = await streamed.result
        timeline = await third.thread.timeline()
        assert isinstance(timeline, Ok)
        inputs = [e.event for e in timeline.value.entries if isinstance(e.event, UserInputEvent)]
        assert [i.data.text for i in inputs] == ["a", "b", "c"]

    asyncio.run(main())


def test_an_unknown_thread_id_is_a_config_error() -> None:
    async def main() -> None:
        bot = agent(model=scripted_model({"responses": []}))
        missing = ThreadId("0192a000-0000-7000-8000-00000000dead")
        with pytest.raises(ConfigError) as refused:
            await bot.run("a", store=sqlite(":memory:"), thread=missing)
        assert refused.value.code == "invalid_config"

    asyncio.run(main())
