"""A local MCP server for the adapter tests, on the SDK's FastMCP: run as a stdio script, or
served in process over Streamable HTTP through an ASGI transport. No network either way."""

import atexit
import os
import signal
import sys
from collections.abc import Mapping
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

SEEN_HEADERS: list[Mapping[str, str]] = []


def server(*, resources: bool = False) -> FastMCP:
    app = FastMCP("kit", stateless_http=True, json_response=True)

    if resources:

        @app.resource("kit://greeting")
        def greeting() -> str:
            """A greeting resource."""
            return "hello from kit"

    @app.tool()
    def echo(text: str) -> str:
        """Echo text back."""
        return f"echo: {text}"

    @app.tool(name="sendEmail")
    def send_email(to: str) -> str:
        """Send an email."""
        return f"sent to {to}"

    @app.tool()
    def crash() -> str:
        """Dies mid-call, after the request was received."""
        sys.stdout.flush()
        os._exit(1)

    return app


class TracedAsgi(httpx.AsyncBaseTransport):
    """An ASGI transport that fires the connection's send-point trace, as httpcore does."""

    def __init__(self, app: FastMCP) -> None:
        self._inner = httpx.ASGITransport(app=app.streamable_http_app())

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        SEEN_HEADERS.append(dict(request.headers))
        trace = request.extensions.get("trace")
        if trace is not None:
            await trace("http11.send_request_headers.started", {})
        return await self._inner.handle_async_request(request)


def _log_connection(path: str) -> None:
    """One process is one connection: "open" when it starts, "closed" when it exits, however."""

    def log(line: str) -> None:
        with Path(path).open("a") as f:
            f.write(f"{line}\n")

    def stop(_signum: int, _frame: object) -> None:
        sys.exit(0)

    log("open")
    atexit.register(log, "closed")
    signal.signal(signal.SIGTERM, stop)


if __name__ == "__main__":
    connections = os.environ.get("CONNECTIONS_FILE")
    if connections is not None:
        _log_connection(connections)
    server().run("stdio")
