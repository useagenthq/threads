"""`daytona()`: sandboxes on Daytona through its official SDK's generated async clients
(`daytona_api_client_async`, `daytona_toolbox_api_client_async`), every request on one aiohttp
session fenced at the send point (transport.py). The SDK's high-level `AsyncDaytona` is not
used: some of its paths open their own httpx clients, which can't be fenced.

What this adapter declares (Daytona 0.216, container sandboxes):
- Create: the sandbox is named `threads-<operation key>`. Names are unique per organization,
  so a repeated create answers 409 and is the same sandbox, and lookup is a GET by name.
  Lookup is nonfinal: a create that passed its fence may still be in flight at the provider.
- Expiry: `lifetime_ms` becomes Daytona's `ttlMinutes`, after which it destroys the sandbox.
- Private, with a leak backstop: `public` is always false, and an idle sandbox auto-stops after
  `auto_stop_minutes` (default 60) and is auto-deleted as long after. The ledger owns cleanup.
- The toolbox runs as the image's non-root user, so create makes /workspace with sudo.
- Egress: denied by default (`networkBlockAll`, enforced by Daytona); `allow_internet=True`
  lifts it and the adapter declares egress unenforced.
- Close deletes and waits until Daytona reports the sandbox destroyed.
- Exec streams stdout and stderr (the toolbox's WebSocket log stream) and never buffers whole.
- Termination is unconfirmed: deleting the command's session is Daytona's only kill, and
  nothing proves a command's detached descendants are gone, so terminate answers unknown.
- Snapshots are cold only: stop (all processes end), capture, start. That is the only
  quiescence Daytona proves for containers (no pause; a hot snapshot is VM/Windows-only), so a
  snapshot is disruptive: the parent's processes don't survive it. Capture class filesystem;
  no provider expiry is declared. A snapshot can't be found by key with its manifest, so
  snapshot lookup is none. Restore verifies the manifest and deletes a mismatched child.
- The manifest a snapshot returns is the parent's just before the stop, and must equal the
  parent's after the start (the remote kit, sandbox/remote/session.py): still a claim. Core
  proves it against the image by a ledgered restore before recording the snapshot, and
  refuses one whose image differs (thread/snapshot.py).
- The sandbox, session and snapshot logic is the remote kit's; this adapter is its driver
  (driver.py) over Daytona's clients (clients.py).
"""

import re
from collections.abc import Sequence

import aiohttp

from threads.adapters.loop_resources import LoopResources
from threads.adapters.sandboxes.daytona.clients import Clients, close_clients, open_clients
from threads.adapters.sandboxes.daytona.control import Placement
from threads.adapters.sandboxes.daytona.driver import DaytonaDriver
from threads.agents.config import ConfigError
from threads.sandbox.remote.sandbox import RemoteInfo, RemoteSandbox
from threads.secrets import Secret, credential

DEFAULT_API = "https://app.daytona.io/api"
API_KEY = "DAYTONA_API_KEY"
HOUR_MS = 3_600_000
_PROVIDER = re.compile(r"[a-z][a-z0-9_]{0,63}")


class DaytonaSandbox(RemoteSandbox):
    """spec/api.json `Sandbox` on Daytona (module docstring)."""

    def __init__(  # noqa: PLR0913 - the factory's options
        self,
        api_key: str | Secret | None,
        *,
        api_url: str = DEFAULT_API,
        snapshot: str | None = None,
        target: str | None = None,
        lifetime_ms: int | None = None,
        allow_internet: bool = False,
        auto_stop_minutes: int = 60,
        name: str = "daytona",
        poll_s: float = 1.0,
        wait_s: float = 300.0,
        traces: Sequence[aiohttp.TraceConfig] = (),
    ) -> None:
        if _PROVIDER.fullmatch(name) is None:
            raise ConfigError("invalid_config", f"provider name {name!r}: [a-z][a-z0-9_]*")
        if auto_stop_minutes <= 0:
            raise ConfigError("invalid_config", f"auto_stop_minutes {auto_stop_minutes}: > 0")
        # Daytona reads a TTL of 0 as "no TTL": a sandbox declared expired would live on.
        if lifetime_ms is not None and lifetime_ms <= 0:
            raise ConfigError("invalid_config", f"daytona: lifetime_ms {lifetime_ms}: > 0")
        self.lifetime_ms: int | None = lifetime_ms
        """The declared provider expiry of a created sandbox, when set."""
        ttl = None if lifetime_ms is None else -(-lifetime_ms // 60_000)
        placed = Placement(
            target, ttl, auto_stop_minutes=auto_stop_minutes, block_network=not allow_internet
        )
        self._key = credential("daytona", "api_key", api_key, API_KEY)
        self._api_url, self._pacing, self._traces = api_url, (poll_s, wait_s), traces
        self._clients = LoopResources(name, close_clients)
        driver = DaytonaDriver(self._loop_clients, snapshot, placed, self._pacing)
        super().__init__(driver, RemoteInfo(name, "unenforced" if allow_internet else "enforced"))

    async def setup(self) -> None:
        """Resolves the key on the host. The HTTP session is opened on first use, in the run
        and on its event loop, and closed when nothing holds that loop any more."""
        self._key()

    def _loop_clients(self) -> Clients:
        return self._clients.get(
            lambda: open_clients(self._key(), self._api_url, self._traces, *self._pacing)
        )


def daytona(  # noqa: PLR0913 - the provider's options
    *,
    api_key: str | Secret | None = None,
    api_url: str = DEFAULT_API,
    snapshot: str | None = None,
    target: str | None = None,
    lifetime_ms: int = HOUR_MS,
    allow_internet: bool = False,
    auto_stop_minutes: int = 60,
    name: str = "daytona",
) -> DaytonaSandbox:
    """A Daytona sandbox provider (the `daytona()` of spec/api.json conventions.adapters).
    `api_key` defaults to `secret("DAYTONA_API_KEY")`, resolved at setup; `snapshot` is the
    base snapshot to create from."""
    return DaytonaSandbox(
        api_key,
        api_url=api_url,
        snapshot=snapshot,
        target=target,
        lifetime_ms=lifetime_ms,
        allow_internet=allow_internet,
        auto_stop_minutes=auto_stop_minutes,
        name=name,
    )
