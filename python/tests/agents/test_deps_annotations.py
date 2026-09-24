"""How a run decides deps may be omitted: only the context parameter's annotation is read
(threads/_tool_deps.py). This module uses string annotations on purpose."""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING

import pytest
from agents.test_deps_default import ALLOW, USAGE
from pydantic import BaseModel

from threads import Completed, ConfigError, RunContext, agent, scripted_model, sqlite, tool
from threads._tool_deps import context_deps, missing_deps

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pydantic import JsonValue

type NoDeps = RunContext[None]


def _script() -> dict[str, JsonValue]:
    call: JsonValue = {"type": "tool_use", "call_id": "c1", "name": "look", "input": {"q": "x"}}
    return {
        "responses": [
            {"content": [call], "stop_reason": "tool_use", "usage": USAGE},
            {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": USAGE,
            },
        ]
    }


def test_a_local_input_class_and_a_type_checking_import_dont_hide_the_context() -> None:
    seen: list[object] = []

    class Local(BaseModel):
        q: str

    async def look(_args: Local, ctx: RunContext[None]) -> JsonValue:
        seen.append(ctx.deps)
        return "found"

    looker = tool(name="look", description="Look.", input=Local, execute=look, effect="read_only")
    bot = agent(model=scripted_model(_script()), tools=[looker], permissions=ALLOW)
    assert isinstance(asyncio.run(bot.run("go", store=sqlite(":memory:"))), Completed)
    assert seen == [None]


class Query(BaseModel):
    q: str


async def _look(_args: Query, ctx: RunContext[None], *, prefix: str) -> str:
    return prefix + str(ctx.deps)


def test_a_partial_runs_without_deps() -> None:
    looker = tool(
        name="look",
        description="Look.",
        input=Query,
        execute=functools.partial(_look, prefix="p"),
        effect="read_only",
    )
    bot = agent(model=scripted_model(_script()), tools=[looker], permissions=ALLOW)
    assert isinstance(asyncio.run(bot.run("go", store=sqlite(":memory:"))), Completed)


def _logged[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    @functools.wraps(fn)
    async def inner(*args: P.args, **kwargs: P.kwargs) -> R:
        return await fn(*args, **kwargs)

    return inner


def _bare[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    async def inner(*args: P.args, **kwargs: P.kwargs) -> R:
        return await fn(*args, **kwargs)

    return inner


async def _aliased(_args: Query, _ctx: NoDeps) -> str:
    return "ok"


async def _needs(_args: Query, _ctx: RunContext[int]) -> RunContext[None]:
    raise AssertionError


async def _unannotated(_args: Query, _ctx) -> str:  # noqa: ANN001 - the missing annotation under test  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType] - under test
    return "ok"


async def _hidden(_args: Query, _ctx: Unknown[None]) -> str:  # noqa: F821 - an unresolvable name under test  # pyright: ignore[reportUndefinedVariable, reportUnknownParameterType] - under test
    return "ok"


CASES: list[tuple[object, str]] = [
    (_aliased, "none"),
    (_logged(_aliased), "none"),
    (functools.partial(_look, prefix="p"), "none"),
    (_needs, "deps"),  # a RunContext[None] return annotation doesn't count
    (_bare(_aliased), "unreadable"),
    (_unannotated, "unreadable"),
    (_hidden, "unreadable"),
    ("not callable", "unreadable"),
]


@pytest.mark.parametrize(("execute", "expected"), CASES)
def test_context_deps_reads_only_the_context_parameter(execute: object, expected: str) -> None:
    assert context_deps(execute) == expected


def test_an_unreadable_context_says_so_in_the_error() -> None:
    looker = tool(name="look", description="Look.", input=Query, execute=_bare(_aliased))
    bot = agent(model=scripted_model(_script()), tools=[looker], permissions=ALLOW)

    async def main() -> None:
        await bot.run("go", store=sqlite(":memory:"))

    with pytest.raises(ConfigError, match="couldn't read the context annotation of tool look"):
        asyncio.run(main())
    assert "deps=None" in missing_deps("a", [("look", "unreadable")])
    assert missing_deps("a", [("look", "deps")]).endswith("pass run(..., deps=...)")
