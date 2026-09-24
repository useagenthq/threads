"""One event loop's Daytona clients: the fenced HTTP session, the control client over it, each
sandbox's toolbox client, and the exec streams reading it. Opened on first use in a run, closed
when nothing holds that loop any more (adapters/loop_resources.py)."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import partial

import aiohttp

from threads.adapters.loop_resources import close_all, drain
from threads.adapters.sandboxes.daytona import transport
from threads.adapters.sandboxes.daytona.control import Control, client
from threads.adapters.sandboxes.daytona.toolbox import Toolbox

_PUMP_GRACE_S = 0.25


@dataclass(frozen=True, slots=True)
class Clients:
    session: aiohttp.ClientSession
    control: Control
    pumps: set[asyncio.Task[None]]
    toolboxes: dict[str, Toolbox] = field(default_factory=dict[str, Toolbox])
    """By sandbox id: each at the proxy URL its sandbox record gives."""


def open_clients(
    api_key: str, api_url: str, traces: Sequence[aiohttp.TraceConfig], poll_s: float, wait_s: float
) -> Clients:
    session = transport.session(api_key, traces)
    return Clients(session, Control(client(api_url, session), poll_s, wait_s), set())


async def close_clients(clients: Clients) -> None:
    """Closes the session, then the exec streams still reading it. The session goes first:
    closing it closes every connection it holds or is opening, so a pump mid-request ends with
    an error rather than being cancelled in a connect that leaves a socket behind."""
    # A pump still running after the grace waits on a reader that is gone, not the network.
    await close_all([clients.session.close, partial(drain, clients.pumps, _PUMP_GRACE_S)])
