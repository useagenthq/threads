"""The Anthropic adapter, offline: golden request bodies for conformance render cases, stream
mapping, rejections, and one transport attempt per send."""

import asyncio
import json

import httpx2
import pytest
from fakes import FakeContext, Script, collect, golden, line, render_case, sse
from pydantic import JsonValue

from threads.adapters.models.anthropic.model import AnthropicModel
from threads.anthropic import anthropic
from threads.log import CallId, ReasoningPart, TextPart, ToolUsePart, Usage
from threads.loop.model import Delta, Done, ModelChunk, PartChunk, Rejected

type Ev = tuple[str, dict[str, JsonValue]]
USAGE: dict[str, JsonValue] = {"input_tokens": 10, "output_tokens": 1}
WINDOW = 200_000
END: Ev = ("message_stop", {})


def begin(usage: dict[str, JsonValue] = USAGE) -> Ev:
    return ("message_start", {"message": {"id": "msg_1", "usage": usage}})


def start(index: int, block: dict[str, JsonValue]) -> Ev:
    return ("content_block_start", {"index": index, "content_block": block})


def delta(index: int, kind: str, value: str) -> Ev:
    key = {"text": "text", "thinking": "thinking", "signature": "signature"}.get(kind)
    body: dict[str, JsonValue] = {"type": f"{kind}_delta", key or "partial_json": value}
    return ("content_block_delta", {"index": index, "delta": body})


def stop(index: int) -> Ev:
    return ("content_block_stop", {"index": index})


def finish(reason: str, usage: dict[str, JsonValue]) -> Ev:
    return ("message_delta", {"delta": {"stop_reason": reason}, "usage": usage})


TEXT: dict[str, JsonValue] = {"type": "text", "text": ""}
USE: dict[str, JsonValue] = {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}


def reply(*events: Ev) -> httpx2.Response:
    return sse([(name, {"type": name, **data}) for name, data in events])


def model(script: Script) -> AnthropicModel:
    declared = anthropic("claude-test", max_input_tokens=WINDOW, max_output_tokens=4096).info
    return AnthropicModel(declared, "sk-test-1", http=httpx2.MockTransport(script))


def one_turn() -> bytes:
    head: JsonValue = {
        "adapter": {"name": "anthropic", "settings": {}, "version": "1"},
        "model": {"name": "claude-test", "provider": "anthropic"},
        "params": {"max_tokens": 64},
        "system": "Be brief.",
        "tools": [],
    }
    return line(head) + line({"role": "user", "content": [{"type": "text", "text": "hi"}]})


def run(script: Script, body: bytes, context: FakeContext | None = None) -> list[ModelChunk]:
    return asyncio.run(collect(model(script).send, body, context or FakeContext()))


@pytest.mark.parametrize(
    "case",
    [
        "render-thinking-block-replay",
        "render-user-image-input",
        "render-screenshot-tool-result",
        "render-deferred-tool-loaded",
    ],
)
def test_a_render_case_maps_to_its_golden_request(case: str) -> None:
    body, context = render_case(case, "anthropic", "anthropic")
    script = Script([reply(begin(), start(0, TEXT), delta(0, "text", "ok"), stop(0), END)])
    run(script, body, context)
    golden(f"anthropic/{case}.json", script.bodies()[0])


def test_hosted_tool_parts_are_refused_before_dispatch() -> None:
    body, context = render_case("render-hosted-search-citations", "anthropic", "anthropic")
    # A hosted part another provider recorded can't go back to this one.
    ours = b'"name":"web_search","provider":"anthropic"'
    body = body.replace(ours, b'"name":"web_search","provider":"scripted"')
    script = Script([])
    assert run(script, body, context) == [Rejected("continuation_unsupported")]
    assert script.sent == []


def test_another_providers_reasoning_is_refused_not_dropped() -> None:
    body, context = render_case("render-thinking-block-replay", "anthropic", "anthropic")
    body = body.replace(b'"provider":"anthropic","ref"', b'"provider":"openai","ref"')
    assert run(Script([]), body, context) == [Rejected("continuation_unsupported")]


def test_an_epoch_pinned_to_another_adapter_is_refused() -> None:
    body = one_turn().replace(b'"name":"anthropic"', b'"name":"openai"', 1)
    assert run(Script([]), body) == [Rejected("continuation_unsupported")]


def test_the_stream_maps_to_deltas_parts_and_usage() -> None:
    usage = {**USAGE, "cache_read_input_tokens": 7, "cache_creation_input_tokens": 0}
    events: list[Ev] = [
        begin(usage),
        start(0, {"type": "thinking", "thinking": ""}),
        delta(0, "thinking", "Plan."),
        delta(0, "signature", "sig"),
        stop(0),
        ("ping", {}),
        start(1, USE),
        delta(1, "input_json", '{"path"'),
        delta(1, "input_json", ':"a"}'),
        stop(1),
        finish("tool_use", {"output_tokens": 9}),
        END,
    ]
    context = FakeContext()
    reasoning, call, done = run(Script([reply(*events)]), one_turn(), context)
    assert isinstance(reasoning, PartChunk)
    assert isinstance(reasoning.part, ReasoningPart)
    stored = json.loads(context.artifacts[reasoning.part.ref.sha256])
    assert stored == {"type": "thinking", "thinking": "Plan.", "signature": "sig"}
    assert reasoning.part.provider == "anthropic"
    args: dict[str, JsonValue] = {"path": "a"}
    use = ToolUsePart(type="tool_use", call_id=CallId("toolu_1"), name="read_file", input=args)
    assert call == PartChunk(use)
    counted = Usage(input_tokens=10, output_tokens=9, cache_read_tokens=7, cache_write_tokens=0)
    assert done == Done("tool_use", counted)


def test_text_streams_as_deltas_and_missing_usage_stays_unknown() -> None:
    events = [
        begin({"input_tokens": 4}),
        start(0, TEXT),
        delta(0, "text", "a"),
        delta(0, "text", "b"),
        stop(0),
        finish("end_turn", {}),
        END,
    ]
    chunks = run(Script([reply(*events)]), one_turn())
    assert chunks[:3] == [Delta(0, "a"), Delta(0, "b"), PartChunk(TextPart(type="text", text="ab"))]
    unknown = Usage(
        input_tokens=4, output_tokens=None, cache_read_tokens=None, cache_write_tokens=None
    )
    assert chunks[3] == Done("end_turn", unknown)


def test_a_tool_call_cut_off_by_max_tokens_never_exists() -> None:
    events = [
        begin(),
        start(0, TEXT),
        delta(0, "text", "a"),
        stop(0),
        start(1, USE),
        delta(1, "input_json", '{"pa'),
        stop(1),
        finish("max_tokens", USAGE),
        END,
    ]
    chunks = run(Script([reply(*events)]), one_turn())
    assert [type(c) for c in chunks] == [Delta, PartChunk, Done]
    assert isinstance(chunks[-1], Done)
    assert chunks[-1].stop_reason == "max_tokens"


@pytest.mark.parametrize(
    ("status", "headers", "body", "expected"),
    [
        (429, {"retry-after": "2"}, "slow down", Rejected("rate_limited", 429, 2000)),
        (529, {}, "overloaded", Rejected("overloaded", 529)),
        (500, {}, "boom", Rejected("server_error", 500)),
        (400, {}, "prompt is too long: 300000 tokens", Rejected("prompt_too_long", 400)),
        (401, {}, "bad key", Rejected("provider_error", 401)),
    ],
)
def test_an_http_rejection_is_a_rejected_chunk_after_one_attempt(
    status: int, headers: dict[str, str], body: str, expected: Rejected
) -> None:
    error = {"type": "error", "error": {"type": "x", "message": body}}
    script = Script([httpx2.Response(status, headers=headers, json=error)])
    assert run(script, one_turn()) == [expected]
    assert len(script.sent) == 1


def _raises(error: type[httpx2.TransportError]) -> AnthropicModel:
    def handle(request: httpx2.Request) -> httpx2.Response:
        raise error("down", request=request)

    info = anthropic("claude-test", max_input_tokens=WINDOW, max_output_tokens=10).info
    return AnthropicModel(info, "sk-test-1", http=httpx2.MockTransport(handle))


def test_a_refused_connection_is_server_error_and_a_broken_read_is_uncertain() -> None:
    refused = asyncio.run(collect(_raises(httpx2.ConnectError).send, one_turn(), FakeContext()))
    assert refused == [Rejected("server_error")]
    with pytest.raises(Exception, match="Connection error"):
        asyncio.run(collect(_raises(httpx2.ReadError).send, one_turn(), FakeContext()))


def test_a_stream_error_before_content_is_a_rejection_and_after_content_uncertain() -> None:
    error: Ev = ("error", {"error": {"type": "overloaded_error", "message": "busy"}})
    assert run(Script([reply(begin(), error)]), one_turn()) == [Rejected("overloaded")]
    late = Script([reply(start(0, TEXT), delta(0, "text", "a"), error)])
    with pytest.raises(Exception, match="overloaded_error"):
        run(late, one_turn())


def test_the_factory_declares_its_limits_and_no_lookup() -> None:
    made = anthropic(
        "claude-test", max_input_tokens=WINDOW, max_output_tokens=8192, params={"temperature": 0}
    )
    assert made.info.limits.context_window == WINDOW
    assert made.info.params == {"max_tokens": 8192, "temperature": 0}
    assert made.info.lookup == "none"
    assert made.info.adapter.name == "anthropic"


def test_the_client_uses_the_key_setup_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-lane09-at-setup")
    declared = anthropic("claude-test", max_input_tokens=WINDOW, max_output_tokens=4096).info
    script = Script([reply(begin(), start(0, TEXT), delta(0, "text", "ok"), stop(0), END)])
    claude = AnthropicModel(declared, None, http=httpx2.MockTransport(script))
    asyncio.run(claude.setup())
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    asyncio.run(collect(claude.send, one_turn(), FakeContext()))
    assert script.sent[0].headers["x-api-key"] == "sk-lane09-at-setup"


def test_a_delta_names_its_committed_part_index() -> None:
    events = [
        begin({"input_tokens": 4}),
        start(0, {"type": "thinking", "thinking": ""}),
        delta(0, "thinking", "Plan."),
        stop(0),
        start(1, TEXT),
        delta(1, "text", "Hi"),
        stop(1),
        finish("end_turn", {}),
        END,
    ]
    chunks = run(Script([reply(*events)]), one_turn(), FakeContext())
    parts = [c.part for c in chunks if isinstance(c, PartChunk)]
    deltas = [c for c in chunks if isinstance(c, Delta)]
    assert deltas == [Delta(1, "Hi")]
    assert parts[1] == TextPart(type="text", text="Hi")
