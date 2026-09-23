"""The fence at the real send point, over a real connection to a loopback server: a writer that
lost its lease while the SDK prepared or queued the request writes no byte of it."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx2
import pytest
from fakes import FakeContext, collect, line
from pydantic import JsonValue

from threads.adapters.models import transport
from threads.adapters.models.anthropic.model import AnthropicModel, client
from threads.anthropic import anthropic
from threads.loop.model import Done, ModelChunk

_SSE = (
    b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
    b'{"input_tokens":1,"output_tokens":1}}}\n\n'
    b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    b'"usage":{"output_tokens":1}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)
_BODY: JsonValue = {
    "adapter": {"name": "anthropic", "settings": {}, "version": "1"},
    "model": {"name": "claude-test", "provider": "anthropic"},
    "params": {"max_tokens": 8},
    "system": "",
    "tools": [],
}


@asynccontextmanager
async def server(received: list[bytes]) -> AsyncGenerator[int]:
    """Records every byte a connection sends and answers one SSE response per request."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(65536)
        received.append(data)
        if data:
            head = b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
            writer.write(head + f"content-length: {len(_SSE)}\r\n\r\n".encode() + _SSE)
            await writer.drain()
        writer.close()

    listening = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield listening.sockets[0].getsockname()[1]
    finally:
        listening.close()
        await listening.wait_closed()


def _send(context: FakeContext) -> tuple[list[ModelChunk], list[bytes]]:
    async def main() -> tuple[list[ModelChunk], list[bytes]]:
        received: list[bytes] = []
        async with server(received) as port:
            info = anthropic("claude-test", context_window=1000, max_output_tokens=8).info
            model = AnthropicModel(info, client("key", f"http://127.0.0.1:{port}"))
            body = line(_BODY) + line({"role": "user", "content": [{"type": "text", "text": "hi"}]})
            chunks = await collect(model.send, body, context)
            await asyncio.sleep(0.05)
            return chunks, received

    return asyncio.run(main())


def test_the_owner_sends_after_one_fence_at_the_send_point() -> None:
    context = FakeContext()
    chunks, received = _send(context)
    assert isinstance(chunks[-1], Done)
    assert context.fences == 1
    assert received
    assert received[0].startswith(b"POST /v1/messages")


def test_a_lease_lost_while_the_sdk_prepared_the_request_sends_no_byte() -> None:
    context = FakeContext(owner=False)
    chunks, received = _send(context)
    assert chunks == []
    assert context.fences == 1
    assert b"".join(received) == b""


def test_a_request_outside_an_attempt_is_refused() -> None:
    fenced = transport.client(httpx2.MockTransport(_unreachable))
    with pytest.raises(transport.StaleOwnerError):
        asyncio.run(fenced.get("http://127.0.0.1:9/"))


def _unreachable(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f"sent {request.url}")
