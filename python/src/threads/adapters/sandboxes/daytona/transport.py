"""The one aiohttp session every Daytona request goes through, REST and WebSocket alike.

Its trace fences at aiohttp's send point, in `BaseConnector.connect` once the request holds a
pool slot (past any queueing for one) and before `ClientRequest.send` writes its first byte
(aiohttp 3.14, client.py `_connect_and_send_request`): `on_connection_reuseconn` for a pooled
connection, `on_connection_create_start` for a new one. The latter runs before the TCP and TLS
handshake rather than after it, because aiohttp leaks the new socket when a
`create_end` hook raises; a handshake writes no request bytes. A failed fence raises, so nothing
of the request is written.
"""

from collections.abc import Sequence
from types import SimpleNamespace

import aiohttp

from threads.adapters.sandboxes import fence


async def _fence(
    _session: aiohttp.ClientSession, _context: SimpleNamespace, _params: object
) -> None:
    await fence.check()


def trace() -> aiohttp.TraceConfig:
    config = aiohttp.TraceConfig()
    config.on_connection_create_start.append(_fence)
    config.on_connection_reuseconn.append(_fence)
    return config


def session(api_key: str, extra: Sequence[aiohttp.TraceConfig] = ()) -> aiohttp.ClientSession:
    """A session that fences every request and authenticates it to the Daytona API and its
    toolbox proxy (host side; the key never enters a sandbox). `extra` traces are for tests.
    Built inside the running loop: aiohttp binds its connector to it."""
    auth = {"Authorization": f"Bearer {api_key}"}
    return aiohttp.ClientSession(headers=auth, trace_configs=[trace(), *extra])
