"""A UI stream on the wire: one SSE message per frame, its data the frame's chunk as canonical
JSON (RFC 8785) and its id the frame's `<seq>:<k>` when it has one. An AI SDK stream that ends
cleanly sends `data: [DONE]`; a broken one just closes, so the client falls back to a replay."""

from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from typing import Final

from threads.host.ui.frame import Protocol
from threads.host.ui.stream import End, Out
from threads.log.jcs import canonicalize
from threads.result import Ok

HEADERS: Final[Mapping[Protocol, Mapping[str, str]]] = {
    "ai-sdk": {
        "cache-control": "no-cache",
        "x-vercel-ai-ui-message-stream": "v1",
        "x-accel-buffering": "no",
    },
    "ag-ui": {"cache-control": "no-cache", "x-accel-buffering": "no"},
}


async def sse(protocol: Protocol, frames: AsyncGenerator[Out]) -> AsyncIterator[str]:
    try:
        async for out in frames:
            if isinstance(out, End):
                if out.how == "done" and protocol == "ai-sdk":
                    yield "data: [DONE]\n\n"
                return
            data = canonicalize(dict(out.data))
            if not isinstance(data, Ok):
                raise AssertionError(f"a frame is JSON: {data.error}")
            head = "" if out.id is None else f"id: {out.id}\n"
            yield f"{head}data: {data.value}\n\n"
    finally:
        await frames.aclose()
