"""The one aiohttp session every Daytona request goes through, REST and WebSocket alike.

Its trace fences at aiohttp's send point: `on_connection_create_start` fires in
`BaseConnector.connect` once the request holds a pool slot (past any queueing for one) and
before `ClientRequest.send` writes its first byte (aiohttp 3.14, client.py
`_connect_and_send_request`). A failed fence raises there, so nothing of the request is written
and no socket is open yet.

Connections are never reused (`force_close`): aiohttp's hook for a reused connection pops it
from the pool and, when the hook raises, never closes it, so a refused fence would leak it.
ponytail: one TCP and TLS handshake per request; pool again if aiohttp closes a connection whose
reuse hook raised.
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
    return config


def session(api_key: str, extra: Sequence[aiohttp.TraceConfig] = ()) -> aiohttp.ClientSession:
    """A session that fences every request and authenticates it to the Daytona API and its
    toolbox proxy (host side; the key never enters a sandbox). `extra` traces are for tests.
    Built inside the running loop: aiohttp binds its connector to it."""
    # Daytona's log stream marks stdout and stderr only for a client that names its SDK
    # version (the pinned client's); without it both arrive unmarked and can't be told apart.
    auth = {"Authorization": f"Bearer {api_key}", "X-Daytona-SDK-Version": "0.216.0"}
    connector = aiohttp.TCPConnector(force_close=True)
    return aiohttp.ClientSession(connector=connector, headers=auth, trace_configs=[trace(), *extra])
