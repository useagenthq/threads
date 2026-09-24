"""A fake OTLP/HTTP collector on 127.0.0.1 for the exporter tests: it records every request and
answers with scripted statuses (200 by default), or hangs. No real collector is ever reached."""

import asyncio
import gzip
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class Received:
    status: int
    headers: dict[str, str]
    body: bytes
    """The request body as sent (gzip-compressed when content-encoding says so)."""

    @property
    def json_body(self) -> bytes:
        return gzip.decompress(self.body) if self.headers.get("content-encoding") else self.body

    def spans(self) -> list[dict[str, object]]:
        doc = json.loads(self.json_body)
        out: list[dict[str, object]] = []
        for rs in doc["resourceSpans"]:
            for ss in rs["scopeSpans"]:
                out.extend(ss["spans"])
        return out


@dataclass
class Collector:
    url: str = ""
    received: list[Received] = field(default_factory=list[Received])
    statuses: list[int] = field(default_factory=list[int])
    """Answers for the next requests, in order; 200 once they run out."""
    hang: bool = False

    def span_ids(self) -> list[str]:
        """Every span id the collector accepted (a 2xx), in arrival order."""
        return [str(s["spanId"]) for r in self.accepted() for s in r.spans()]

    def accepted(self) -> list[Received]:
        return [r for r in self.received if r.status == 200]  # noqa: PLR2004 - HTTP OK


async def _read(reader: asyncio.StreamReader) -> tuple[dict[str, str], bytes]:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode().split("\r\n")[1:]
    headers = {
        k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines if ln)
    }
    body = await reader.readexactly(int(headers.get("content-length", "0")))
    return headers, body


@asynccontextmanager
async def collector() -> AsyncGenerator[Collector]:
    c = Collector()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            headers, body = await _read(reader)
            if c.hang:
                await asyncio.sleep(3600)
            status = c.statuses.pop(0) if c.statuses else 200
            c.received.append(Received(status, headers, body))
            text = b"bad spans" if status != 200 else b"{}"  # noqa: PLR2004 - HTTP OK
            writer.write(
                f"HTTP/1.1 {status} X\r\ncontent-length: {len(text)}\r\n"
                "connection: close\r\n\r\n".encode()
                + text
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    c.url = f"http://127.0.0.1:{port}/v1/traces"
    try:
        yield c
    finally:
        server.close()
