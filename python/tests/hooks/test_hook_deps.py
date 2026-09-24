"""Hooks and extension tools get the run's deps, typed by the agent's Deps, as in TypeScript
(spec/api.json Hooks ctx: RunContext<Deps>; extension tools are app tools)."""

import asyncio
from dataclasses import dataclass
from typing import assert_type

import pytest
from pydantic import BaseModel, JsonValue

from threads import (
    Agent,
    Completed,
    ConfigError,
    RunContext,
    agent,
    extension,
    scripted_model,
    sqlite,
    tool,
)
from threads.hooks.types import Source
from threads.log import Permissions

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
ALLOW = Permissions(
    mode="default",
    allow=["ops__look"],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=False,
    plan_exit_mode="default",
)


@dataclass(frozen=True)
class Deps:
    user: str


class Empty(BaseModel):
    pass


def _script() -> dict[str, JsonValue]:
    part: JsonValue = {"type": "tool_use", "call_id": "call_1", "name": "ops__look", "input": {}}
    use: JsonValue = {"content": [part], "stop_reason": "tool_use", "usage": USAGE}
    done: JsonValue = {
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": USAGE,
    }
    return {"responses": [use, done]}


def _bot(seen: list[str]) -> Agent[Deps, str]:
    async def start(source: Source, ctx: RunContext[Deps]) -> list[str]:
        seen.append(f"{source}:{ctx.deps.user}")
        return []

    async def look(_args: Empty, ctx: RunContext[Deps]) -> str:
        seen.append(f"tool:{ctx.deps.user}")
        return "looked"

    ops = extension(
        name="ops",
        hooks={"session_start": start},
        tools=[tool(name="look", description="Look.", input=Empty, runs="host", execute=look)],
    )
    bot = agent(model=scripted_model(_script()), permissions=ALLOW, extensions=[ops])
    assert_type(bot, Agent[Deps, str])
    return bot


def test_a_hook_and_an_extension_tool_see_the_deps_passed_to_run() -> None:
    seen: list[str] = []

    async def main() -> None:
        result = await _bot(seen).run("go", store=sqlite(":memory:"), deps=Deps("bob"))
        assert isinstance(result, Completed)

    asyncio.run(main())
    assert seen == ["startup:bob", "tool:bob"]


def test_an_agent_whose_hooks_read_deps_needs_them() -> None:
    async def main() -> None:
        bot = _bot([])
        with pytest.raises(ConfigError) as refused:
            await bot.run("go", store=sqlite(":memory:"))  # pyright: ignore[reportCallIssue] - deps omitted on purpose
        assert refused.value.code == "invalid_config"

    asyncio.run(main())
