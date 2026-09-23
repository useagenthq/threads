"""The transports the E2B SDK's clients send through, supplied by threads and fenced at their
send points (adapters/sandboxes/fence.py):

- httpx (the control-plane API and envd's file API): at the connection's request-header write,
  the httpcore trace, after the pool handed out a connection. A request that reaches the
  network without that trace would be unfenced, so it fails loudly as an adapter bug.
- pyqwest (envd's connect RPC for processes): at the transport's `execute`, the last Python
  frame before reqwest. reqwest's pool is Rust with no Python hook; over one HTTP/2 connection
  per sandbox its only wait is for that connection, and a request that fails the fence never
  enters it.
"""

from collections.abc import Mapping

import httpx
import pyqwest

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
        await self._inner.aclose()


class FencedPyqwest(pyqwest.Transport):
    def __init__(self, inner: pyqwest.Transport) -> None:
        self._inner = inner

    async def execute(self, request: pyqwest.Request) -> pyqwest.Response:
        await fence.check()
        return await self._inner.execute(request)


def http_transport() -> httpx.AsyncBaseTransport:
    """The real httpx transport: httpcore, which emits the header trace."""
    return httpx.AsyncHTTPTransport(retries=0)


def rpc_transport() -> pyqwest.Transport:
    """The real pyqwest transport: no retries (a lost answer is resolved by the ledger)."""
    return pyqwest.HTTPTransport(tls_include_system_certs=True, follow_redirects=False)
