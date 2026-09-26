"""The httpx transport every adapter's clients send through, fenced at httpcore's send point:
the connection's request-header write, after the pool handed out a connection. A response that
arrived without that trace would be unfenced, so it fails loudly as an adapter bug."""

from collections.abc import Mapping

import httpx

from threads.adapters.sandboxes import fence


class FencedHttpx(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        written: list[str] = []

        async def trace(event: str, _info: Mapping[str, object]) -> None:
            if event.endswith(".send_request_headers.started"):
                await fence.check()
                written.append(event)

        request.extensions["trace"] = trace
        response = await self._inner.handle_async_request(request)
        if not written:
            await response.aclose()
            raise fence.UnfencedRequestError("the inner transport sent without its header trace")
        return response

    async def aclose(self) -> None:
        """Leaves the inner transport open: the clients of one event loop share it, and that
        loop's release closes it once, after them."""
