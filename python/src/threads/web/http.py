"""The host's HTTP/1.1 transport for the web tools and the git gateway's forge calls. Stdlib only
(core's one dependency is pydantic). It connects to the address the guard checked, and awaits
the run's fence after connecting and before writing a byte: a writer that lost its lease sends
nothing."""

import asyncio
import ssl
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol

from threads.result import Err, Ok
from threads.web.guard import Target

type Fence = Callable[[], Awaitable[bool]]
type WebErrorCode = Literal["stale_epoch", "timeout", "unavailable"]

TIMEOUT_S: Final = 30.0
HEAD_BYTES: Final = 64 * 1024


@dataclass(frozen=True, slots=True)
class WebError:
    code: WebErrorCode
    message: str
    sent: bool
    """Whether any request byte may have reached the server."""


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: Mapping[str, str]
    """Lowercase names; a repeated header keeps its last value."""
    body: bytes
    truncated: bool = False
    """The body went past the cap and was cut there."""


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    body: bytes | None = None


class Transport(Protocol):
    async def send(
        self, target: Target, request: Request, fence: Fence, max_bytes: int
    ) -> Ok[Response] | Err[WebError]: ...


class StdlibTransport:
    """One connection per request (`Connection: close`), no compression, no redirects: the
    caller vets and follows each hop."""

    def __init__(self, timeout_s: float = TIMEOUT_S) -> None:
        self._timeout = timeout_s

    async def send(
        self, target: Target, request: Request, fence: Fence, max_bytes: int
    ) -> Ok[Response] | Err[WebError]:
        tls = ssl.create_default_context() if target.scheme == "https" else None
        try:
            async with asyncio.timeout(self._timeout):
                reader, writer = await asyncio.open_connection(
                    target.ip, target.port, ssl=tls, server_hostname=target.host if tls else None
                )
        except (OSError, TimeoutError) as error:
            return Err(WebError("unavailable", f"connect {target.host}: {error}", sent=False))
        try:
            if not await fence():
                return Err(WebError("stale_epoch", "this run no longer owns the branch", False))
            writer.write(_head(target, request) + (request.body or b""))
            async with asyncio.timeout(self._timeout):
                await writer.drain()
                return Ok(await _read(reader, max_bytes))
        except TimeoutError:
            return Err(WebError("timeout", f"{target.host} did not answer in time", sent=True))
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError) as e:
            return Err(WebError("unavailable", f"{target.host}: {e}", sent=True))
        finally:
            writer.close()


def _head(target: Target, request: Request) -> bytes:
    default = 443 if target.scheme == "https" else 80
    host = f"[{target.host}]" if ":" in target.host else target.host
    lines = [
        f"{request.method} {target.path} HTTP/1.1",
        f"Host: {host}" if target.port == default else f"Host: {host}:{target.port}",
        "Connection: close",
        "Accept-Encoding: identity",
        "User-Agent: threads",
    ]
    lines += [f"{k}: {v}" for k, v in request.headers.items()]
    if request.body is not None:
        lines.append(f"Content-Length: {len(request.body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


async def _read(reader: asyncio.StreamReader, max_bytes: int) -> Response:
    head = await reader.readuntil(b"\r\n\r\n")
    if len(head) > HEAD_BYTES:
        raise ValueError("response head too large")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    status = int(status_line.split(" ", 2)[1])
    headers: dict[str, str] = {}
    for line in header_lines:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body, cut = await _chunked(reader, max_bytes)
    elif "content-length" in headers:
        size = int(headers["content-length"])
        body, cut = await reader.readexactly(min(size, max_bytes)), size > max_bytes
    else:
        body = await reader.read(max_bytes + 1)
        while len(body) <= max_bytes and (more := await reader.read(max_bytes + 1 - len(body))):
            body += more
        body, cut = body[:max_bytes], len(body) > max_bytes
    return Response(status, headers, body, cut)


async def _chunked(reader: asyncio.StreamReader, max_bytes: int) -> tuple[bytes, bool]:
    body = bytearray()
    while True:
        size = int((await reader.readline()).split(b";", 1)[0].strip() or b"0", 16)
        if size == 0:
            return bytes(body), False
        if len(body) + size > max_bytes:
            body += await reader.readexactly(max_bytes - len(body))
            return bytes(body), True
        body += await reader.readexactly(size)
        await reader.readline()
