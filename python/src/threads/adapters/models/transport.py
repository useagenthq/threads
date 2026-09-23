"""The HTTP layer the provider SDKs send through: fenced at the real send point, one attempt.

An SDK may prepare, queue for a pooled connection, or wait on auth before any byte leaves. The
lease can move in that time, so the fence runs where the request's first byte is written: the
connection's `send_request_headers` trace, after the pool handed out a connection (spec/api.json `Model.send`). A failed fence raises there and nothing is written.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from http import HTTPStatus

import httpx2

from threads.loop.model import ModelChunk, ModelContext, Rejected
from threads.result import Err

_OVERLOADED = 529
"""Anthropic's overloaded status."""
_attempt: ContextVar[ModelContext | None] = ContextVar("threads_model_attempt", default=None)


class StaleOwnerError(Exception):
    """The fence failed at the send point: this writer no longer owns the branch."""


class FencedTransport(httpx2.AsyncBaseTransport):
    """Wraps a transport so every request re-checks the current attempt's fence at its send
    point. A request outside an attempt is refused: an adapter never sends unfenced."""

    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        context = _attempt.get()
        if context is None:
            raise StaleOwnerError("a model request outside an attempt")
        request.extensions["trace"] = _tracer(context)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


type _Trace = Callable[[str, Mapping[str, object]], Awaitable[None]]


def _tracer(context: ModelContext) -> _Trace:
    async def trace(event: str, _info: Mapping[str, object]) -> None:
        if event.endswith(".send_request_headers.started") and isinstance(
            await context.fence(), Err
        ):
            raise StaleOwnerError(f"lease lost before sending (epoch {context.epoch})")

    return trace


def client(transport: httpx2.AsyncBaseTransport | None = None) -> httpx2.AsyncClient:
    """The SDK's HTTP client. `transport` is for tests; the default opens real connections."""
    return httpx2.AsyncClient(transport=FencedTransport(transport or httpx2.AsyncHTTPTransport()))


@contextmanager
def attempt(context: ModelContext) -> Generator[None]:
    """Binds the fence for the requests made inside it. Held only around the SDK call that
    sends, never across a yield, so it can't leak into the consumer's context."""
    token = _attempt.set(context)
    try:
        yield
    finally:
        _attempt.reset(token)


def stale(error: BaseException) -> bool:
    """The SDK wraps a transport exception in its own connection error: find ours."""
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, StaleOwnerError):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


def not_sent(error: BaseException) -> bool:
    """A connection that failed before the request was written (connection
    refused is server_error). Anything later is uncertainty."""
    cause = error.__cause__
    return isinstance(cause, httpx2.ConnectError | httpx2.ConnectTimeout)


def rejection(status: int, headers: Mapping[str, str], too_long: bool) -> Rejected:
    """a provider's HTTP rejection before any content."""
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        return Rejected("rate_limited", status, _retry_after_ms(headers))
    if status in (HTTPStatus.SERVICE_UNAVAILABLE, _OVERLOADED):
        return Rejected("overloaded", status)
    if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        return Rejected("server_error", status)
    if too_long:
        return Rejected("prompt_too_long", status)
    return Rejected("provider_error", status)


def _retry_after_ms(headers: Mapping[str, str]) -> int | None:
    millis = headers.get("retry-after-ms")
    if millis is not None and millis.isdigit():
        return int(millis)
    seconds = headers.get("retry-after")
    if seconds is not None and seconds.isdigit():
        return int(seconds) * 1000
    return None


@dataclass(frozen=True, slots=True)
class Sse:
    event: str | None
    data: str


async def sse(response: httpx2.Response) -> AsyncIterator[Sse]:
    """Server-sent events of a streamed response (the field subset providers use)."""
    event: str | None = None
    data: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif not line and data:
            yield Sse(event, "\n".join(data))
            event, data = None, []
    if data:
        yield Sse(event, "\n".join(data))


async def relay[E](
    response: object,
    parse: Callable[[str], E | None],
    feed: Callable[[E], Awaitable[Sequence[ModelChunk]]],
    rejected: Callable[[Exception], Rejected | None],
) -> AsyncIterator[ModelChunk]:
    """Streams a provider's events as chunks. An error event before any chunk is a rejection
    when `rejected` names one; after content, or unnamed, it is uncertainty and raises."""
    if not isinstance(response, httpx2.Response):
        raise TypeError("the SDK returned no raw response")
    started = False
    try:
        async for event in sse(response):
            parsed = parse(event.data)
            if parsed is None:
                continue
            try:
                chunks = await feed(parsed)
            except Exception as error:
                rejection = None if started else rejected(error)
                if rejection is None:
                    raise
                yield rejection
                return
            for chunk in chunks:
                started = True
                yield chunk
    finally:
        await response.aclose()
