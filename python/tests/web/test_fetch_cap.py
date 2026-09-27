"""Where web_fetch cuts a response, against the shared vector
(spec/conformance/vectors/web-fetch-cap.json).

The cap is one number for both languages (spec/schema/limits.json, generated into each), because it
is the truncation boundary: cut at a different byte and the same URL leaves a different content hash
in the log. The TypeScript twin is packages/core/test/tools/web-cap.test.ts, over the same vector.
"""

import asyncio
import json
from hashlib import sha256
from pathlib import Path
from typing import Final

import pytest
from pydantic import TypeAdapter

from threadsai._generated.limits import WEB_FETCH_MAX_BYTES
from threadsai.result import Err, Ok
from threadsai.web.fetch import get
from threadsai.web.guard import Target
from threadsai.web.http import Fence, Request, Response, StdlibTransport, WebError

_FILE = Path(__file__).resolve().parents[3] / "spec/conformance/vectors/web-fetch-cap.json"
_VECTOR = json.loads(_FILE.read_text())
_CASES: Final = TypeAdapter(list[tuple[str, int, int, str]]).validate_python(
    [(c["name"], c["served"], c["kept"], c["sha256"]) for c in _VECTOR["cases"]]
)
_CAP: Final = TypeAdapter(int).validate_python(_VECTOR["max_bytes"])


async def _open() -> bool:
    return True


def _body(n: int) -> bytes:
    """Byte i is i % 251, as the vector's generator builds it."""
    cycle = bytes(range(251))
    return (cycle * (n // len(cycle) + 1))[:n]


async def _serve(body: bytes) -> tuple[asyncio.Server, int]:
    head = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n\r\n" % len(body)

    async def handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            writer.write(head + body)
            await writer.drain()
        except OSError:
            # A cut page: the reader stops at the cap and closes, so the rest never lands.
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def test_the_cap_the_code_uses_is_the_vectors() -> None:
    assert WEB_FETCH_MAX_BYTES == _CAP


@pytest.mark.parametrize(
    ("served", "kept", "digest"), [c[1:] for c in _CASES], ids=[c[0] for c in _CASES]
)
def test_a_page_is_cut_at_the_cap_and_hashes_to_the_pinned_value(
    served: int, kept: int, digest: str
) -> None:
    asyncio.run(_fetched(served, kept, digest))


async def _fetched(served: int, kept: int, digest: str) -> None:
    server, port = await _serve(_body(served))
    async with server:
        target = Target("http://example.com/p", "http", "example.com", port, "/p", "127.0.0.1")
        got = await StdlibTransport().send(target, Request("GET"), _open, WEB_FETCH_MAX_BYTES)
    assert isinstance(got, Ok)
    page = got.value
    assert len(page.body) == kept
    assert sha256(page.body).hexdigest() == digest
    assert page.truncated == (served > kept)


class _Cap:
    """A transport that answers nothing and records the cap it was handed."""

    def __init__(self) -> None:
        self.max_bytes: int | None = None

    async def send(
        self, target: Target, request: Request, fence: Fence, max_bytes: int
    ) -> Ok[Response] | Err[WebError]:
        self.max_bytes = max_bytes
        return Ok(Response(200, {"content-type": "text/plain"}, b"hi"))


def test_a_fetch_hands_the_transport_that_cap() -> None:
    async def main() -> None:
        transport = _Cap()

        async def resolve(_host: str, _port: int) -> list[str]:
            return ["93.184.216.34"]

        assert isinstance(await get("https://example.com/", resolve, transport, _open), Ok)
        assert transport.max_bytes == WEB_FETCH_MAX_BYTES

    asyncio.run(main())
