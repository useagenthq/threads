"""One event loop's E2B clients: the transports, the control client over them, and every
sandbox's envd clients opened on that loop. Made on first use in a run, closed when nothing
holds that loop any more (adapters/loop_resources.py)."""

from dataclasses import dataclass, field

from e2b.api import AsyncApiClient
from e2b.connection_config import ConnectionConfig

from threads.adapters.loop_resources import close_all
from threads.adapters.sandboxes.e2b.control import Control
from threads.adapters.sandboxes.e2b.envd import Envd, Transports
from threads.adapters.sandboxes.e2b.transport import FencedHttpx


@dataclass(frozen=True, slots=True)
class Plane:
    transports: Transports
    config: ConnectionConfig
    client: AsyncApiClient
    control: Control
    envds: dict[str, Envd] = field(default_factory=dict[str, Envd])
    """By sandbox id, until each is closed."""


def open_plane(config: ConnectionConfig, transports: Transports) -> Plane:
    client = AsyncApiClient(config, transport=FencedHttpx(transports.http))
    return Plane(transports, config, client, Control(client))


async def close_plane(plane: Plane) -> None:
    """Every envd (its exec streams, then its clients), the control client, then the
    transports they all share; each is attempted even if one before it fails."""
    envds = [envd.aclose for envd in list(plane.envds.values())]
    control = plane.client.get_async_httpx_client().aclose
    await close_all([*envds, control, plane.transports.aclose])
