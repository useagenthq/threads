"""Provider-hosted tools through the model adapters: only web search and
fetch are allowed at setup, they are pinned in line 0 and sent as declared, each use is a
hosted_tool part holding the exact provider block, and it goes back exactly as recorded."""

import asyncio
import json

import httpx2
import pytest
from fakes import FakeContext, Script, collect, line, sse
from pydantic import JsonValue

from threads.adapters.models.anthropic.model import AnthropicModel
from threads.adapters.models.openai.model import OpenAIModel
from threads.agents.config import ConfigError
from threads.anthropic import anthropic
from threads.log import CitationPart, HostedToolPart, TextPart
from threads.loop.model import PartChunk
from threads.openai import openai

WEB_SEARCH: dict[str, JsonValue] = {"type": "web_search_20250305", "name": "web_search"}


@pytest.mark.parametrize(
    "tool",
    [
        {"type": "code_execution_20250522", "name": "code_execution"},
        {"type": "computer_20250124", "name": "computer"},
        {"type": "mcp", "server_label": "x"},
        {"name": "untyped"},
        {"type": "web_search_mcp", "name": "web_search"},
        {"type": "web_fetch_20250910_exec", "name": "web_fetch"},
    ],
)
def test_anthropic_refuses_effectful_hosted_tools_at_setup(tool: dict[str, JsonValue]) -> None:
    with pytest.raises(ConfigError) as raised:
        anthropic("claude-test", hosted_tools=[tool], context_window=1, max_output_tokens=1)
    assert raised.value.code == "hosted_tool_unsupported"


@pytest.mark.parametrize(
    "kind",
    [
        "code_interpreter",
        "file_search",
        "mcp",
        "computer_use_preview",
        "image_generation",
        "web_search_mcp",
    ],
)
def test_openai_refuses_effectful_hosted_tools_at_setup(kind: str) -> None:
    with pytest.raises(ConfigError) as raised:
        openai(
            "gpt-test",
            hosted_tools=[{"type": kind}],
            context_window=1,
            max_output_tokens=1,
            api_key="sk-test-1",
        )
    assert raised.value.code == "hosted_tool_unsupported"


def test_allowed_hosted_tools_are_pinned_in_adapter_settings() -> None:
    fetch: dict[str, JsonValue] = {"type": "web_fetch_20250910", "name": "web_fetch"}
    info = anthropic("c", hosted_tools=[WEB_SEARCH, fetch], context_window=1, max_output_tokens=1)
    assert info.info.adapter.settings == {"hosted_tools": [WEB_SEARCH, fetch]}
    assert info.info.hosted_tools == ("web_search", "web_fetch")
    oa = openai(
        "g",
        hosted_tools=[{"type": "web_search"}],
        context_window=1,
        max_output_tokens=1,
        api_key="sk-test-1",
    )
    assert oa.info.hosted_tools == ("web_search",)
    plain = anthropic("c", context_window=1, max_output_tokens=1)
    assert plain.info.adapter.settings == {}
    assert plain.info.hosted_tools == ()


def _head(adapter: str, provider: str, hosted: list[JsonValue]) -> bytes:
    head: JsonValue = {
        "adapter": {"name": adapter, "settings": {"hosted_tools": hosted}, "version": "1"},
        "model": {"name": "m", "provider": provider},
        "params": {"max_tokens": 64},
        "system": "",
        "tools": [],
    }
    return line(head) + line({"role": "user", "content": [{"type": "text", "text": "q"}]})


def _reply(events: list[tuple[str, dict[str, JsonValue]]]) -> httpx2.Response:
    return sse([(n, {"type": n, **d}) for n, d in events])


def test_anthropic_server_tool_blocks_become_hosted_parts_and_replay_exactly() -> None:
    use: dict[str, JsonValue] = {
        "type": "server_tool_use",
        "id": "srvtoolu_1",
        "name": "web_search",
    }
    results: dict[str, JsonValue] = {
        "type": "web_search_tool_result",
        "tool_use_id": "srvtoolu_1",
        "content": [{"type": "web_search_result", "url": "https://bun.sh/", "title": "Bun"}],
    }
    cite: dict[str, JsonValue] = {
        "type": "web_search_result_location",
        "url": "https://bun.sh/",
        "title": "Bun",
        "cited_text": "Bun 1.3",
        "encrypted_index": "x",
    }
    events: list[tuple[str, dict[str, JsonValue]]] = [
        ("message_start", {"message": {"usage": {"input_tokens": 1}}}),
        ("content_block_start", {"index": 0, "content_block": {**use, "input": {}}}),
        (
            "content_block_delta",
            {"index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"query":"bun"}'}},
        ),
        ("content_block_stop", {"index": 0}),
        ("content_block_start", {"index": 1, "content_block": results}),
        ("content_block_stop", {"index": 1}),
        ("content_block_start", {"index": 2, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"index": 2, "delta": {"type": "text_delta", "text": "Out."}}),
        (
            "content_block_delta",
            {"index": 2, "delta": {"type": "citations_delta", "citation": cite}},
        ),
        ("content_block_stop", {"index": 2}),
        ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
        ("message_stop", {}),
    ]
    script = Script([_reply(events)])
    declared = anthropic(
        "m",
        hosted_tools=[WEB_SEARCH],
        context_window=1,
        max_output_tokens=1,
        api_key="sk-test-1",
    ).info
    model = AnthropicModel(declared, "sk-test-1", http=httpx2.MockTransport(script))
    context = FakeContext()
    chunks = asyncio.run(
        collect(model.send, _head("anthropic", "anthropic", [WEB_SEARCH]), context)
    )
    first_body = script.bodies()[0]
    assert isinstance(first_body, dict)
    assert first_body["tools"] == [WEB_SEARCH]
    parts = [c.part for c in chunks if isinstance(c, PartChunk)]
    first, second, text, citation = parts
    assert isinstance(first, HostedToolPart)
    assert isinstance(second, HostedToolPart)
    assert (first.name, first.format, second.format) == (
        "web_search",
        "server_tool_use",
        "web_search_tool_result",
    )
    assert json.loads(context.artifacts[first.ref.sha256]) == {**use, "input": {"query": "bun"}}
    assert isinstance(text, TextPart)
    assert isinstance(citation, CitationPart)
    assert citation.source_id == "https://bun.sh/"
    # The next request carries the recorded blocks back byte for byte.
    assistant = [json.loads(p.model_dump_json()) for p in parts]
    body = _head("anthropic", "anthropic", [WEB_SEARCH]) + line(
        {"role": "assistant", "content": assistant}
    )
    body += line({"role": "user", "content": [{"type": "text", "text": "more"}]})
    again = Script([_reply([events[0], events[-2], events[-1]])])
    replay = AnthropicModel(declared, "sk-test-1", http=httpx2.MockTransport(again))
    asyncio.run(collect(replay.send, body, context))
    sent = again.bodies()[0]
    assert isinstance(sent, dict)
    messages = sent["messages"]
    assert isinstance(messages, list)
    assert messages[1] == {
        "role": "assistant",
        "content": [{**use, "input": {"query": "bun"}}, results, {"type": "text", "text": "Out."}],
    }


def test_openai_hosted_items_become_hosted_parts() -> None:
    call: dict[str, JsonValue] = {
        "type": "web_search_call",
        "id": "ws_1",
        "status": "completed",
        "action": {"type": "search", "query": "bun"},
    }
    done: dict[str, JsonValue] = {
        "type": "response.completed",
        "response": {"usage": {"input_tokens": 1, "output_tokens": 1}},
    }
    script = Script(
        [sse([(None, {"type": "response.output_item.done", "item": call}), (None, done)])]
    )
    declared = openai(
        "m",
        hosted_tools=[{"type": "web_search"}],
        context_window=1,
        max_output_tokens=1,
        api_key="sk-test-1",
    ).info
    model = OpenAIModel(declared, "sk-test-1", http=httpx2.MockTransport(script))
    context = FakeContext()
    chunks = asyncio.run(
        collect(model.send, _head("openai", "openai", [{"type": "web_search"}]), context)
    )
    sent = script.bodies()[0]
    assert isinstance(sent, dict)
    assert sent["tools"] == [{"type": "web_search"}]
    (part,) = [c.part for c in chunks if isinstance(c, PartChunk)]
    assert isinstance(part, HostedToolPart)
    assert (part.provider, part.format) == ("openai", "web_search_call")
    assert json.loads(context.artifacts[part.ref.sha256]) == call
