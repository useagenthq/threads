"""Every adapter frames a late tool result with the same marker TypeScript sends (lane 10):
`[late tool result: call_id=<id>]` before the result's content, in a user message."""

import asyncio
from collections.abc import Callable, Coroutine

import pytest
from fakes import FakeContext, line
from pydantic import JsonValue

from threads.adapters.models.anthropic.request import build as anthropic_build
from threads.adapters.models.litellm.request import build as litellm_build
from threads.adapters.models.openai.request import build as openai_build
from threads.adapters.models.render import Request, parse
from threads.loop.model import ModelContext

MARKER = "[late tool result: call_id=call_0]"

type Build = Callable[[Request, ModelContext], Coroutine[None, None, dict[str, JsonValue]]]


def _render(adapter: str) -> bytes:
    head: JsonValue = {
        "adapter": {"name": adapter, "version": "1", "settings": {}},
        "model": {"provider": adapter, "name": "m"},
        "params": {"max_tokens": 64},
        "system": "",
        "tools": [],
    }
    late: JsonValue = {
        "role": "tool",
        "call_id": "call_0",
        "is_error": False,
        "late": True,
        "content": [{"type": "text", "text": "finished"}],
    }
    hi: JsonValue = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    said: JsonValue = {"role": "assistant", "content": [{"type": "text", "text": "Waiting."}]}
    return line(head) + line(hi) + line(said) + line(late)


async def _anthropic(request: Request, context: ModelContext) -> dict[str, JsonValue]:
    return (await anthropic_build(request, context)).json


@pytest.mark.parametrize(
    ("adapter", "build", "key"),
    [
        ("anthropic", _anthropic, "messages"),
        ("openai", openai_build, "input"),
        ("litellm", litellm_build, "messages"),
    ],
)
def test_a_late_result_carries_the_shared_marker(adapter: str, build: Build, key: str) -> None:
    body = asyncio.run(build(parse(_render(adapter)), FakeContext()))
    assert MARKER in str(body[key])
