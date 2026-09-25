"""`e2b()`: the E2B sandbox provider (spec/api.json `Sandbox`) over the official `e2b` SDK.

What it declares, and why:
- create: tagged with its operation key in sandbox metadata, found again by a metadata query.
  A create that passed its fence may still be in flight when the query runs, so a miss is
  never final (lookup.create nonfinal).
- no snapshots (capture_classes empty; restore answers snapshot_missing before any call).
  E2B's snapshot pauses the VM, but the manifest a snapshot returns must describe the
  captured image, and nothing can be run inside that pause or against the
  immutable image: a manifest taken before or after it can differ from what was captured
  (a write A->B before, B->A after), and a restored memory image resumes the writer before
  any check could run. Offering snapshots needs such a provider boundary, proven live.
- termination unconfirmed: envd kills the process it started, not what that process detached.
- egress enforced only with the internet off (the default); on, it is unenforced.
- the sandbox dies on its own `lifetime_ms` after create (E2B's timeout): the declared expiry.
- the sandbox and session logic is the remote kit's; this adapter is its driver (driver.py)
  over one event loop's clients (plane.py).
"""

from collections.abc import Callable

from e2b.connection_config import ConnectionConfig

from threads.adapters.loop_resources import LoopResources
from threads.adapters.sandboxes.e2b.driver import E2BDriver, Settings
from threads.adapters.sandboxes.e2b.envd import Transports
from threads.adapters.sandboxes.e2b.plane import Plane, close_plane, open_plane
from threads.adapters.sandboxes.e2b.transport import http_transport, rpc_transport
from threads.agents.config import ConfigError
from threads.sandbox.remote.sandbox import RemoteInfo, RemoteSandbox
from threads.secrets import Secret, credential

HOUR_MS = 3_600_000
API_KEY = "E2B_API_KEY"


class E2BSandbox(RemoteSandbox):
    def __init__(  # noqa: PLR0913 - one provider's settings
        self,
        api_key: str | Secret | None,
        transports: Callable[[], Transports],
        *,
        template: str,
        lifetime_ms: int,
        internet: bool,
        name: str,
        domain: str | None = None,
        api_url: str | None = None,
    ) -> None:
        self._key = credential("e2b", "api_key", api_key, API_KEY)
        self._domain, self._api_url = domain, api_url
        self._planes = LoopResources(name, close_plane)
        self._transports = transports
        """Makes one event loop's transports."""
        self.lifetime_ms = lifetime_ms
        """The declared provider expiry: E2B kills the sandbox this long after create."""
        settings = Settings(template, max(1, lifetime_ms // 1000), internet)
        declared = RemoteInfo(name, "unenforced" if internet else "enforced")
        super().__init__(E2BDriver(self._plane, settings), declared)

    async def setup(self) -> None:
        """Resolves the key on the host. The clients are made on first use, in the run and on
        its event loop, and closed when nothing holds that loop any more."""
        self._key()

    def _plane(self) -> Plane:
        def make() -> Plane:
            config = ConnectionConfig(
                api_key=self._key(), domain=self._domain, api_url=self._api_url, retries=0
            )
            return open_plane(config, self._transports())

        return self._planes.get(make)


def e2b(  # noqa: PLR0913 - the provider's settings
    *,
    api_key: str | Secret | None = None,
    template: str = "base",
    lifetime_ms: int = HOUR_MS,
    allow_internet: bool = False,
    name: str = "e2b",
    domain: str | None = None,
) -> E2BSandbox:
    """An E2B sandbox provider (spec/api.json conventions.adapters). `api_key` defaults to
    `secret("E2B_API_KEY")`, resolved at setup; it authenticates the control plane only and
    never enters a sandbox.
    `template` must carry /bin/sh, env and tar (E2B's base does)."""
    if lifetime_ms <= 0:
        raise ConfigError(
            "invalid_config", f"e2b: lifetime_ms must be a positive number of ms, not {lifetime_ms}"
        )
    return E2BSandbox(
        api_key,
        lambda: Transports(http_transport(), rpc_transport()),
        domain=domain,
        template=template,
        lifetime_ms=lifetime_ms,
        internet=allow_internet,
        name=name,
    )
