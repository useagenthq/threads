"""The LiteLLM bridge, offline: golden `acompletion` arguments, chunk mapping through LiteLLM's
real OpenAI route against a scripted HTTP transport, rejections, retries off, and the fence."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from functools import partial

import httpx2
import openai
import pytest
from fakes import FakeContext, Script, collect, golden, line, render_case
from pydantic import JsonValue

from threads.adapters.models.litellm.model import ACOMPLETION, LiteLLMModel
from threads.adapters.models.render import UnsupportedContentError
from threads.litellm import litellm
from threads.log import CallId, TextPart, ToolUsePart, Usage
from threads.loop.model import Delta, Done, ModelChunk, PartChunk, Rejected

ROUTE = "openai/gpt-test"
INFO = litellm(ROUTE, context_window=128_000, max_output_tokens=4096).info


@dataclass
class Recorder:
    """A stand-in for `acompletion` that keeps its arguments and streams nothing but a stop."""

    calls: list[dict[str, object]] = field(default_factory=list[dict[str, object]])

    async def __call__(self, **kwargs: object) -> AsyncIterator[JsonValue]:
        self.calls.append(kwargs)
        return _chunks([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}])


async def _chunks(items: list[JsonValue]) -> AsyncIterator[JsonValue]:
    for item in items:
        yield item


def one_turn() -> bytes:
    head: JsonValue = {
        "adapter": {"name": "litellm", "settings": {}, "version": "1"},
        "model": {"name": ROUTE, "provider": "litellm"},
        "params": {"max_tokens": 64},
        "system": "Be brief.",
        "tools": [],
    }
    return line(head) + line({"role": "user", "content": [{"type": "text", "text": "hi"}]})


def through_litellm(script: Script) -> LiteLLMModel:
    """LiteLLM's real OpenAI route, its SDK client sending to the scripted transport."""
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(script))
    sdk = openai.AsyncOpenAI(api_key="k", max_retries=0, http_client=http)
    return LiteLLMModel(INFO, partial(ACOMPLETION, client=sdk))


def run(model: LiteLLMModel, body: bytes, context: FakeContext | None = None) -> list[ModelChunk]:
    return asyncio.run(collect(model.send, body, context or FakeContext()))


def chat(*chunks: JsonValue) -> httpx2.Response:
    raw = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
    raw += b"data: [DONE]\n\n"
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=raw)


def chunk(choices: list[JsonValue], **extra: JsonValue) -> JsonValue:
    head: dict[str, JsonValue] = {"id": "c", "object": "chat.completion.chunk", "created": 1}
    return {**head, "model": "m", "choices": choices, **extra}


def delta(value: JsonValue, finish: str | None = None) -> JsonValue:
    return chunk([{"index": 0, "delta": value, "finish_reason": finish}])


@pytest.mark.parametrize("case", ["render-user-image-input", "render-deferred-tool-loaded"])
def test_a_render_case_maps_to_its_golden_arguments(case: str) -> None:
    body, context = render_case(case, "litellm", "litellm")
    recorder = Recorder()
    run(LiteLLMModel(INFO, recorder), body, context)
    (call,) = recorder.calls
    golden(f"litellm/{case}.json", json.loads(json.dumps(call)))


def test_the_bridge_carries_no_continuation_and_no_media_results() -> None:
    for case in ("render-thinking-block-replay", "render-screenshot-tool-result"):
        body, context = render_case(case, "litellm", "litellm")
        recorder = Recorder()
        with pytest.raises(UnsupportedContentError):
            run(LiteLLMModel(INFO, recorder), body, context)
        assert recorder.calls == []


# LiteLLM's own stream handler reads a Pydantic attribute that Pydantic 2.11 deprecates.
@pytest.mark.filterwarnings("ignore::pydantic.warnings.PydanticDeprecatedSince211")
def test_chunks_through_litellm_map_to_deltas_parts_and_usage() -> None:
    call: JsonValue = {
        "index": 0,
        "id": "call_1",
        "type": "function",
        "function": {"name": "ls", "arguments": '{"a"'},
    }
    more: JsonValue = {"index": 0, "function": {"arguments": ":1}"}}
    usage: JsonValue = {
        "prompt_tokens": 50,
        "completion_tokens": 7,
        "total_tokens": 57,
        "prompt_tokens_details": {"cached_tokens": 20},
        "completion_tokens_details": {"reasoning_tokens": 3},
    }
    stream = [
        delta({"role": "assistant", "content": "Hi"}),
        delta({"tool_calls": [call]}),
        delta({"tool_calls": [more]}),
        delta({}, "tool_calls"),
        chunk([], usage=usage),
    ]
    script = Script([chat(*stream)])
    chunks = run(through_litellm(script), one_turn())
    args: dict[str, JsonValue] = {"a": 1}
    assert chunks[0] == Delta("Hi")
    assert chunks[1:3] == [
        PartChunk(TextPart(type="text", text="Hi")),
        PartChunk(ToolUsePart(type="tool_use", call_id=CallId("call_1"), name="ls", input=args)),
    ]
    counted = Usage(
        input_tokens=30,
        output_tokens=7,
        cache_read_tokens=20,
        cache_write_tokens=None,
        reasoning_tokens=3,
    )
    assert chunks[3] == Done("tool_use", counted)
    assert len(script.sent) == 1
    sent = script.bodies()[0]
    assert isinstance(sent, dict)
    assert sent["model"] == "gpt-test"


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        # LiteLLM's OpenAI route drops the provider's headers, so no retry-after: backoff.
        (429, "Rate limit reached", Rejected("rate_limited", 429)),
        (500, "The server had an error", Rejected("server_error", 500)),
        (400, "This model's maximum context length is 8 tokens", Rejected("prompt_too_long", 400)),
        (401, "Incorrect API key provided", Rejected("provider_error", 401)),
    ],
)
def test_a_provider_rejection_is_a_rejected_chunk_after_one_attempt(
    status: int, message: str, expected: Rejected
) -> None:
    error = {"error": {"message": message, "type": "x", "code": None}}
    script = Script([httpx2.Response(status, headers={"retry-after": "2"}, json=error)])
    assert run(through_litellm(script), one_turn()) == [expected]
    assert len(script.sent) == 1


def test_a_lost_lease_calls_nothing() -> None:
    recorder = Recorder()
    assert run(LiteLLMModel(INFO, recorder), one_turn(), FakeContext(owner=False)) == []
    assert recorder.calls == []


def test_credentials_are_passed_per_call_never_pinned() -> None:
    made = litellm(ROUTE, context_window=1000, max_output_tokens=8, api_key="sk-secret")
    assert "sk-secret" not in json.dumps(made.info.params)
    assert made.info.params == {"max_tokens": 8}
    assert made.info.lookup == "none"
