"""One event loop's Modal clients: its channels (one per URL, so a fence listener is registered
once), the control client over the API's, each sandbox's task command router, and the exec
streams reading them. Opened on first use in a run, closed when nothing holds that loop any
more (adapters/loop_resources.py)."""

import asyncio
from dataclasses import dataclass, field

import grpclib.client

from threads.adapters.sandboxes.modal.channel import Connect
from threads.adapters.sandboxes.modal.control import Control
from threads.adapters.sandboxes.modal.router import Router


@dataclass(frozen=True, slots=True)
class Plane:
    connect: Connect
    control: Control
    channels: dict[str, grpclib.client.Channel]
    routers: dict[str, Router] = field(default_factory=dict[str, Router])
    """By sandbox id."""
    pumps: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])

    def router(self, url: str, task_id: str, jwt: str) -> Router:
        if url not in self.channels:
            self.channels[url] = self.connect(url)
        return Router(self.channels[url], task_id, jwt)


async def close_plane(plane: Plane) -> None:
    for channel in plane.channels.values():
        channel.close()
