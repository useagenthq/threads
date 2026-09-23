"""Live gate: one real request per model adapter. Skipped unless THREADS_LIVE=1 and the
adapter's key and model are set; never part of the offline suite (AGENTS.md, Tests).

    THREADS_LIVE=1 ANTHROPIC_API_KEY=... THREADS_LIVE_ANTHROPIC_MODEL=... \\
    OPENAI_API_KEY=... THREADS_LIVE_OPENAI_MODEL=... \\
    THREADS_LIVE_LITELLM_MODEL=provider/model (plus that provider's key) \\
    uv run pytest -m live
"""

import asyncio
import os
from collections.abc import Callable, Generator
from typing import TYPE_CHECKING

import pytest
from fakes import FakeContext, collect, line

from threads.anthropic import anthropic
from threads.litellm import litellm
from threads.loop.guard import block_model_requests
from threads.loop.model import Done, Model, PartChunk
from threads.openai import openai
from threads.reduce.handlers import to_json

if TYPE_CHECKING:
    from pydantic import JsonValue

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def live_gate() -> Generator[None]:
    if os.environ.get("THREADS_LIVE") != "1":
        pytest.skip("live gate: set THREADS_LIVE=1 to send real requests")
    block_model_requests(blocked=False)
    yield
    block_model_requests()


def _needs(*names: str) -> str:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        pytest.skip(f"live gate: {', '.join(missing)} not set")
    return os.environ[names[-1]]


type Make = Callable[[str], Model]

ADAPTERS: dict[str, tuple[tuple[str, ...], Make]] = {
    "anthropic": (
        ("ANTHROPIC_API_KEY", "THREADS_LIVE_ANTHROPIC_MODEL"),
        lambda name: anthropic(name, context_window=200_000, max_output_tokens=64),
    ),
    "openai": (
        ("OPENAI_API_KEY", "THREADS_LIVE_OPENAI_MODEL"),
        lambda name: openai(name, context_window=128_000, max_output_tokens=64),
    ),
    "litellm": (
        ("THREADS_LIVE_LITELLM_MODEL",),
        # A reasoning model (gpt-5) can spend 64 tokens on reasoning alone, with no text.
        lambda name: litellm(name, context_window=32_000, max_output_tokens=1024),
    ),
}


# LiteLLM's own stream handler reads a Pydantic attribute that Pydantic 2.11 deprecates.
@pytest.mark.filterwarnings("ignore::pydantic.warnings.PydanticDeprecatedSince211")
@pytest.mark.parametrize("adapter", sorted(ADAPTERS))
def test_one_real_turn_completes(adapter: str) -> None:
    env, make = ADAPTERS[adapter]
    model = make(_needs(*env))
    info = model.info
    head: JsonValue = {
        "adapter": to_json(info.adapter),
        "model": to_json(info.model),
        "params": dict(info.params),
        "system": "Answer with one word.",
        "tools": [],
    }
    user: JsonValue = {"role": "user", "content": [{"type": "text", "text": "Say ok."}]}
    context = FakeContext()
    chunks = asyncio.run(collect(model.send, line(head) + line(user), context))
    assert isinstance(chunks[-1], Done), chunks
    assert any(isinstance(c, PartChunk) for c in chunks)
    assert context.fences == 1
