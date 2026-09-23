"""The HTTP client every hosted memory provider's SDK sends through: fenced at the real send
point by the fence the framework binds around each provider call (`threads.memory.fence`).

A request outside a provider call, or after the run lost its branch, raises before any byte is
written. SDK retries are off: a provider write that may have happened is uncertainty for the
effect path to settle, never re-sent by the SDK on its own.
"""

from collections.abc import Mapping

import httpx

from threads.memory.fence import check


class FencedProviderTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await check()
        request.extensions["trace"] = _trace
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


async def _trace(event: str, _info: Mapping[str, object]) -> None:
    if event.endswith(".send_request_headers.started"):
        await check()


def fenced_client(inner: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """`inner` is for tests; the default opens real connections."""
    transport = FencedProviderTransport(inner or httpx.AsyncHTTPTransport())
    return httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(30.0))
