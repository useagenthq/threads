"""Tool origins (spec lane 22, test 4e): a pinned extension tool records the extension it came
from. It isn't model-visible, so line 0 is byte-equal with and without it; config_hash covers it,
so agents with extension tools get a new hash (pinned below, old and new), agents without keep
theirs, and continuing an older thread says why it can't."""

import asyncio
from typing import Final

import pytest
from eval_kit import Order, say

from threads import Agent, ConfigError, RunContext, agent, scripted_model, sqlite, tool
from threads.agents.store import now_ms, open_store
from threads.hooks.extension import extension
from threads.log import BranchId, ThreadId, ToolSpec
from threads.loop.drafts import draft
from threads.render.lines import tools_line
from threads.result import Ok
from threads.store.lines import uuid7
from threads.thread.handle import Thread

PLAIN_HASH: Final = "57e16a33d4ed8aba5c50cc95310c37e6e3c030058c15f32ae9bc7f82c9085986"
"""Golden: an agent without extension tools, the same before and after this release."""
EXTENSION_HASH_BEFORE: Final = "555258287ce20be763ab5bb20cb650753564748bcd57b14c5e72a440ba7c7e75"
EXTENSION_HASH_AFTER: Final = "6d861a03800d1d1d4b08ca7757bdfa9da9ea1e478af792a849107fbb3074f216"
"""Goldens: an agent with one extension tool, before and after tool origins. Python's pinned
config differs from TypeScript's (tool schemas come from Pydantic): the hashes are its own."""


async def _note(args: Order, _ctx: RunContext[None]) -> str:
    return args.id


NOTE = tool(
    name="note",
    description="Write a note.",
    input=Order,
    runs="host",
    effect="read_only",
    execute=_note,
)


def plain() -> Agent[None, str]:
    return agent(
        name="support",
        instructions="Help.",
        model=scripted_model({"responses": [say("hi")]}),
        tools=[NOTE],
    )


def extended() -> Agent[None, str]:
    return agent(
        name="support",
        instructions="Help.",
        model=scripted_model({"responses": [say("hi")]}),
        extensions=[extension(name="audit", tools=[NOTE])],
    )


def _without_origin(spec: ToolSpec) -> dict[str, object]:
    return {
        k: v for k, v in spec.model_dump(mode="json", exclude_unset=True).items() if k != "origin"
    }


def test_an_extension_tool_carries_its_origin_and_line_0_is_the_same_without_it() -> None:
    specs = extended().definition.specs()
    note = next(s for s in specs if s.name == "audit__note")
    assert note.origin is not None
    assert note.model_dump(mode="json", exclude_unset=True)["origin"] == {"extension": "audit"}
    bare = [ToolSpec.model_validate(_without_origin(s)) for s in specs]
    assert tools_line(bare) == tools_line(specs)


def test_an_agent_without_extension_tools_keeps_its_config_hash() -> None:
    assert plain().definition.thread_started()["config_hash"] == PLAIN_HASH


def test_an_agent_with_an_extension_tool_gets_a_new_config_hash_on_purpose() -> None:
    definition = extended().definition
    assert definition.thread_started()["config_hash"] == EXTENSION_HASH_AFTER
    assert EXTENSION_HASH_AFTER != EXTENSION_HASH_BEFORE


def test_continuing_a_thread_started_before_origins_says_why_it_cannot() -> None:
    async def body() -> None:
        store = sqlite(":memory:")
        sq = await open_store(store)
        thread, branch = ThreadId(uuid7(now_ms())), BranchId(uuid7(now_ms()))
        assert await sq.create(thread, branch, now_ms()) == Ok(None)
        writer = await sq.acquire(branch, "earlier-release", now_ms)
        assert isinstance(writer, Ok)
        started = dict(extended().definition.thread_started())
        tools = started["tools"]
        assert isinstance(tools, list)
        started["tools"] = [
            {k: v for k, v in t.items() if k != "origin"} for t in tools if isinstance(t, dict)
        ]
        started["config_hash"] = EXTENSION_HASH_BEFORE
        assert isinstance(await writer.value.append([draft("thread_started", started)]), Ok)
        await writer.value.release()
        with pytest.raises(ConfigError) as raised:
            await extended().run("Again.", store=store, thread=Thread(thread, branch, store))
        assert (raised.value.code, raised.value.message) == (
            "invalid_config",
            "this thread was started with another config: tool origin was added in this "
            "release; start a new thread",
        )

    asyncio.run(body())
