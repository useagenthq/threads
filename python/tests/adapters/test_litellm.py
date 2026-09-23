"""The LiteLLM bridge, offline: golden `acompletion` arguments, chunk mapping through LiteLLM's
real OpenAI route against a scripted HTTP transport, rejections, retries off, and the fence."""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from functools import partial
from typing import cast

import httpx2
import openai
import pytest
from fakes import FakeContext, Script, collect, golden, line, render_case
from pydantic import JsonValue

from threads.adapters.models.litellm.model import ACOMPLETION, LiteLLMModel
from threads.agents.config import ConfigError
from threads.litellm import litellm
from threads.log import CallId, TextPart, ToolUsePart, Usage
from threads.loop.model import Delta, Done, ModelChunk, ModelRequest, PartChunk, Rejected

ROUTE = "openai/gpt-test"
INFO = litellm(ROUTE, context_window=128_000, max_output_tokens=4096, api_key="k").info


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
    cases = (
        ("render-thinking-block-replay", Rejected("continuation_unsupported")),
        ("render-screenshot-tool-result", Rejected("content_unsupported")),
    )
    for case, refused in cases:
        body, context = render_case(case, "litellm", "litellm")
        recorder = Recorder()
        assert run(LiteLLMModel(INFO, recorder), body, context) == [refused]
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
        (429, "Rate limit reached", Rejected("rate_limited", 429, 2000)),
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


def test_a_route_it_cannot_fence_at_the_transport_is_refused_at_setup() -> None:
    # A send can carry provider-hosted tools, so a stale send is never harmless: a route whose
    # real transport isn't ours is refused, not shipped with a weaker fence.
    with pytest.raises(ConfigError) as refused:
        litellm("bedrock/some-model", context_window=1000, max_output_tokens=8)
    assert refused.value.code == "transport_fence_unsupported"


def test_credentials_are_passed_per_call_never_pinned() -> None:
    made = litellm(ROUTE, context_window=1000, max_output_tokens=8, api_key="sk-secret")
    assert "sk-secret" not in json.dumps(made.info.params)
    assert made.info.params == {"max_tokens": 8}
    assert made.info.lookup == "none"


class Cut(httpx2.AsyncByteStream):
    """An SSE body that sends its first event, then loses the connection; records its close."""

    def __init__(self, first: JsonValue) -> None:
        self.first = b"data: " + json.dumps(first).encode() + b"\n\n"
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.first
        raise httpx2.ReadError("connection reset")

    async def aclose(self) -> None:
        self.closed = True


def cut(first: JsonValue) -> tuple[Cut, httpx2.Response]:
    body = Cut(first)
    return body, httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=body)


class Garbled(Cut):
    """Its second event isn't JSON; the connection stays open after it."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.first
        yield b"data: {not json\n\n"
        yield self.first


@pytest.mark.filterwarnings("ignore::pydantic.warnings.PydanticDeprecatedSince211")
@pytest.mark.parametrize("failing", [Cut, Garbled])
def test_a_stream_failing_after_the_answer_began_is_raised_and_closed(failing: type[Cut]) -> None:
    # LiteLLM rewraps a mid-stream failure as a status error it makes up (500): the provider
    # answered 200, so it is uncertainty to raise, never a Rejected (the live 500). The stream
    # is closed before send ends, never left to the event loop's shutdown (the live aclose()).
    body = failing(delta({"role": "assistant", "content": ""}))
    response = httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=body)
    model = through_litellm(Script([response]))

    async def main() -> None:
        with pytest.raises(openai.APIError):
            await collect(model.send, one_turn(), FakeContext())
        assert body.closed

    asyncio.run(main())


@pytest.mark.filterwarnings("ignore::pydantic.warnings.PydanticDeprecatedSince211")
def test_a_consumer_that_stops_early_closes_the_stream() -> None:
    body = Garbled(delta({"content": "Hi"}))
    response = httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=body)
    model = through_litellm(Script([response]))

    async def main() -> None:
        # send is an async generator; its declared type (AsyncIterator) has no aclose.
        sent = model.send(ModelRequest("b:e", one_turn()), FakeContext())
        stream = cast("AsyncGenerator[ModelChunk]", sent)
        assert await anext(stream) == Delta("Hi")
        await stream.aclose()
        assert body.closed

    asyncio.run(main())


class Closing:
    """A LiteLLM stream stand-in whose iteration and close can each fail."""

    def __init__(self, items: list[JsonValue], fail: Exception | None = None) -> None:
        self.items = items
        self.fail = fail

    async def __aiter__(self) -> AsyncIterator[JsonValue]:
        for item in self.items:
            yield item
        if self.fail is not None:
            raise self.fail

    async def aclose(self) -> None:
        raise OSError("close failed")


def closing(stream: Closing) -> LiteLLMModel:
    async def complete(**_: object) -> Closing:
        return stream

    return LiteLLMModel(INFO, complete)


def test_a_failing_close_never_replaces_the_stream_error() -> None:
    stopped: list[JsonValue] = [{"choices": [{"delta": {"content": "Hi"}, "finish_reason": None}]}]
    model = closing(Closing(stopped, RuntimeError("mid-stream")))
    with pytest.raises(RuntimeError, match="mid-stream") as raised:
        run(model, one_turn())
    assert any("close failed" in note for note in raised.value.__notes__)


def test_a_failing_close_never_turns_a_completed_stream_into_an_error() -> None:
    done: list[JsonValue] = [{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]
    chunks = run(closing(Closing(done)), one_turn())
    assert chunks[0] == Delta("ok")
    assert isinstance(chunks[-1], Done)
