"""The OpenAI Responses adapter, offline: golden request bodies for conformance render cases,
stream mapping, rejections, and one transport attempt per send."""

import asyncio
import json

import httpx2
import pytest
from fakes import FakeContext, Script, collect, golden, line, render_case, sse
from pydantic import JsonValue

from threads.adapters.models.openai.model import OpenAIModel, client
from threads.log import CallId, ReasoningPart, TextPart, ToolUsePart, Usage
from threads.loop.model import Delta, Done, ModelChunk, PartChunk, Rejected
from threads.openai import openai

type Ev = tuple[str | None, JsonValue]
WINDOW = 400_000
COUNTS: JsonValue = {
    "input_tokens": 100,
    "input_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 10},
    "output_tokens": 20,
    "output_tokens_details": {"reasoning_tokens": 15},
}


def event(kind: str, **data: JsonValue) -> Ev:
    return (None, {"type": kind, **data})


def text(value: str) -> Ev:
    return event("response.output_text.delta", delta=value)


def item(value: dict[str, JsonValue]) -> Ev:
    return event("response.output_item.done", item=value)


def said(value: str) -> Ev:
    content: JsonValue = [{"type": "output_text", "text": value, "annotations": []}]
    return item({"type": "message", "role": "assistant", "content": content})


def end(kind: str = "response.completed", **response: JsonValue) -> Ev:
    return event(kind, response={"usage": COUNTS, **response})


def model(script: Script) -> OpenAIModel:
    declared = openai("gpt-test", context_window=WINDOW, max_output_tokens=4096, api_key="k").info
    return OpenAIModel(declared, client("key", http=httpx2.MockTransport(script)))


def one_turn() -> bytes:
    head: JsonValue = {
        "adapter": {"name": "openai", "settings": {}, "version": "1"},
        "model": {"name": "gpt-test", "provider": "openai"},
        "params": {"max_output_tokens": 64},
        "system": "Be brief.",
        "tools": [],
    }
    return line(head) + line({"role": "user", "content": [{"type": "text", "text": "hi"}]})


def run(script: Script, body: bytes, context: FakeContext | None = None) -> list[ModelChunk]:
    return asyncio.run(collect(model(script).send, body, context or FakeContext()))


@pytest.mark.parametrize(
    "case",
    ["render-user-image-input", "render-screenshot-tool-result", "render-deferred-tool-loaded"],
)
def test_a_render_case_maps_to_its_golden_request(case: str) -> None:
    body, context = render_case(case, "openai", "openai")
    script = Script([sse([said("ok"), end()])])
    run(script, body, context)
    golden(f"openai/{case}.json", script.bodies()[0])


def test_a_recorded_reasoning_item_goes_back_byte_for_byte() -> None:
    context = FakeContext()
    stored = b'{"encrypted_content":"gAAA","id":"rs_1","summary":[],"type":"reasoning"}'
    ref = asyncio.run(context.put(stored, "application/json"))
    reasoning: JsonValue = {
        "type": "reasoning",
        "provider": "openai",
        "model": "gpt-test",
        "format": "openai_reasoning",
        "ref": {"sha256": ref.sha256, "bytes": ref.bytes, "media_type": ref.media_type},
    }
    use: JsonValue = {"type": "tool_use", "call_id": "call_1", "name": "ls", "input": {}}
    done: JsonValue = {"role": "tool", "call_id": "call_1", "is_error": True, "content": []}
    body = one_turn() + line({"role": "assistant", "content": [reasoning, use]}) + line(done)
    script = Script([sse([said("ok"), end()])])
    run(script, body, context)
    golden("openai/reasoning-replay.json", script.bodies()[0])


def test_hosted_tool_parts_and_foreign_reasoning_are_refused_before_dispatch() -> None:
    body, context = render_case("render-hosted-search-citations", "openai", "openai")
    assert run(Script([]), body, context) == [Rejected("continuation_unsupported")]
    body, context = render_case("render-thinking-block-replay", "openai", "openai")
    assert run(Script([]), body, context) == [Rejected("continuation_unsupported")]


def test_the_stream_maps_to_deltas_parts_and_usage() -> None:
    reasoning: dict[str, JsonValue] = {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "gAAA",
        "summary": [{"type": "summary_text", "text": "Look first."}],
    }
    call: dict[str, JsonValue] = {
        "type": "function_call",
        "call_id": "call_9",
        "name": "read_file",
        "arguments": '{"path":"a"}',
        "status": "completed",
    }
    events = [event("response.created", response={}), item(reasoning), text("Hi"), said("Hi")]
    context = FakeContext()
    chunks = run(Script([sse([*events, item(call), end()])]), one_turn(), context)
    first, delta, reply, use, done = chunks
    assert isinstance(first, PartChunk)
    assert isinstance(first.part, ReasoningPart)
    assert json.loads(context.artifacts[first.part.ref.sha256]) == reasoning
    assert first.part.summary == "Look first."
    assert delta == Delta("Hi")
    assert reply == PartChunk(TextPart(type="text", text="Hi"))
    args: dict[str, JsonValue] = {"path": "a"}
    expected = ToolUsePart(type="tool_use", call_id=CallId("call_9"), name="read_file", input=args)
    assert use == PartChunk(expected)
    usage = Usage(
        input_tokens=30,
        output_tokens=20,
        cache_read_tokens=60,
        cache_write_tokens=10,
        reasoning_tokens=15,
    )
    assert done == Done("tool_use", usage)


def test_a_cut_off_call_never_exists_and_missing_usage_stays_unknown() -> None:
    call: dict[str, JsonValue] = {
        "type": "function_call",
        "call_id": "call_9",
        "name": "read_file",
        "arguments": '{"pa',
        "status": "incomplete",
    }
    incomplete = event(
        "response.incomplete", response={"incomplete_details": {"reason": "max_output_tokens"}}
    )
    chunks = run(Script([sse([said("a"), item(call), incomplete])]), one_turn())
    unknown = Usage(
        input_tokens=None,
        output_tokens=None,
        cache_read_tokens=None,
        cache_write_tokens=None,
        reasoning_tokens=None,
    )
    assert chunks == [PartChunk(TextPart(type="text", text="a")), Done("max_tokens", unknown)]


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (429, "rate_limit_exceeded", Rejected("rate_limited", 429, 1000)),
        (503, None, Rejected("overloaded", 503)),
        (500, None, Rejected("server_error", 500)),
        (400, "context_length_exceeded", Rejected("prompt_too_long", 400)),
        (401, "invalid_api_key", Rejected("provider_error", 401)),
    ],
)
def test_an_http_rejection_is_a_rejected_chunk_after_one_attempt(
    status: int, code: str | None, expected: Rejected
) -> None:
    error = {"error": {"message": "no", "type": "x", "code": code}}
    script = Script([httpx2.Response(status, headers={"retry-after-ms": "1000"}, json=error)])
    assert run(script, one_turn()) == [expected]
    assert len(script.sent) == 1


def test_a_failure_before_content_is_a_rejection_and_after_content_uncertain() -> None:
    failed = end("response.failed", error={"code": "server_error", "message": "boom"})
    assert run(Script([sse([failed])]), one_turn()) == [Rejected("server_error")]
    with pytest.raises(Exception, match="server_error"):
        run(Script([sse([text("a"), failed])]), one_turn())
    policy = end("response.failed", error={"code": "invalid_prompt", "message": "no"})
    assert run(Script([sse([policy])]), one_turn()) == [Rejected("provider_error")]


def test_the_factory_declares_its_limits_and_no_lookup() -> None:
    made = openai("gpt-test", context_window=WINDOW, max_output_tokens=8192, api_key="k")
    assert made.info.limits.context_window == WINDOW
    assert made.info.params == {"max_output_tokens": 8192}
    assert made.info.lookup == "none"
