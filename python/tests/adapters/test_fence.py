"""The fence at the real send point, over a real connection to a loopback server: a writer that
lost its lease while the SDK (or LiteLLM) prepared or queued the request writes no byte of it."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx2
import pytest
from fakes import FakeContext, collect, line

from threads.adapters.models import transport
from threads.adapters.models.anthropic.model import AnthropicModel
from threads.anthropic import anthropic
from threads.litellm import litellm
from threads.loop.model import Done, Model, ModelChunk, Rejected
from threads.reduce.handlers import to_json

if TYPE_CHECKING:
    from pydantic import JsonValue

_ANTHROPIC = (
    b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
    b'{"input_tokens":1,"output_tokens":1}}}\n\n'
    b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    b'"usage":{"output_tokens":1}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)
_CHAT = (
    b'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"m","choices":'
    b'[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)


def _claude(url: str) -> Model:
    info = anthropic("claude-test", context_window=1000, max_output_tokens=8).info
    return AnthropicModel(info, "key", url)


def _bridged(url: str) -> Model:
    return litellm(
        "openai/gpt-test", context_window=1000, max_output_tokens=8, api_key="k", base_url=url
    )


ADAPTERS: dict[str, tuple[Callable[[str], Model], bytes, str]] = {
    "anthropic": (_claude, _ANTHROPIC, ""),
    "litellm-openai-route": (_bridged, _CHAT, "/v1"),
}


@asynccontextmanager
async def server(received: list[bytes], reply: bytes) -> AsyncGenerator[int]:
    """Records every byte a connection sends and answers one SSE response per request."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(65536)
        received.append(data)
        if data:
            head = b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
            writer.write(head + f"content-length: {len(reply)}\r\n\r\n".encode() + reply)
            await writer.drain()
        writer.close()

    listening = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield listening.sockets[0].getsockname()[1]
    finally:
        listening.close()
        await listening.wait_closed()


def _send(adapter: str, context: FakeContext) -> tuple[list[ModelChunk], list[bytes]]:
    make, reply, suffix = ADAPTERS[adapter]

    async def main() -> tuple[list[ModelChunk], list[bytes]]:
        received: list[bytes] = []
        async with server(received, reply) as port:
            model = make(f"http://127.0.0.1:{port}{suffix}")
            info = model.info
            head: JsonValue = {
                "adapter": to_json(info.adapter),
                "model": to_json(info.model),
                "params": dict(info.params),
                "system": "",
                "tools": [],
            }
            user: JsonValue = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
            chunks = await collect(model.send, line(head) + line(user), context)
            await asyncio.sleep(0.05)
            return chunks, received

    return asyncio.run(main())


@pytest.mark.parametrize("adapter", sorted(ADAPTERS))
def test_the_owner_sends_after_one_fence_at_the_send_point(adapter: str) -> None:
    context = FakeContext()
    chunks, received = _send(adapter, context)
    assert isinstance(chunks[-1], Done)
    assert context.fences == 1
    assert received
    assert received[0].startswith(b"POST /v1/")


@pytest.mark.parametrize("adapter", sorted(ADAPTERS))
def test_a_lease_lost_while_the_request_was_prepared_sends_no_byte(adapter: str) -> None:
    context = FakeContext(owner=False)
    chunks, received = _send(adapter, context)
    assert chunks == [Rejected("stale_epoch")]
    assert context.fences == 1
    assert b"".join(received) == b""


def test_a_request_outside_an_attempt_is_refused() -> None:
    fenced = transport.client(httpx2.MockTransport(_unreachable))
    with pytest.raises(transport.StaleOwnerError):
        asyncio.run(fenced.get("http://127.0.0.1:9/"))


def _unreachable(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f"sent {request.url}")
