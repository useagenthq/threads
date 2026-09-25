"""`modal()` (spec/api.json `Sandbox`): Modal sandboxes over the official SDK's generated gRPC
protocol (modal_proto), on grpclib channels this adapter opens and fences.

Why not the high-level `modal.Sandbox`: it runs on synchronicity's own event loop in a thread and
opens its channels internally, so no fence could sit at its send point. The generated stubs on
our own channels give one asyncio loop and a fence at every request's headers.

What Modal 1.5.5 supports here, and nothing broader (#170):
- create is idempotent on the operation key: the sandbox is named `threads-<key>` and Modal
  refuses a second running sandbox of one name (ALREADY_EXISTS). Lookup by that name is
  nonfinal: a create already past its fence may still land.
- the sandbox's declared expiry is `lifetime_ms` (Modal's sandbox timeout).
- terminate is unconfirmed: the task command router has no kill for an exec and no view of its
  descendants. SandboxTerminate(wait) ends the whole sandbox, which is `close`.
- no snapshots: the filesystem snapshot ends the sandbox, can't run during an exec and freezes
  nothing, so it can't be a quiescent capture of a parent that goes on; memory snapshots are
  experimental in the SDK. capture_classes is empty; restore answers snapshot_missing.
- egress is enforced deny-all (Modal's block_network) unless `allow_internet`, then
  unenforced.

The sandbox and session logic is the remote kit's; this adapter is its driver (driver.py) over
one event loop's channels (plane.py).
"""

from threads.adapters.loop_resources import LoopResources
from threads.adapters.sandboxes.modal.channel import Connect
from threads.adapters.sandboxes.modal.channel import connect as tls_channel
from threads.adapters.sandboxes.modal.control import Control, Settings
from threads.adapters.sandboxes.modal.driver import ModalDriver
from threads.adapters.sandboxes.modal.plane import Plane, close_plane
from threads.agents.config import ConfigError
from threads.sandbox.remote.sandbox import RemoteInfo, RemoteSandbox
from threads.secrets import Secret, credential

SERVER_URL = "https://api.modal.com"
_MIN_LIFETIME_MS, _MAX_LIFETIME_MS = 1000, 24 * 3600 * 1000


class ModalSandbox(RemoteSandbox):
    def __init__(
        self,
        settings: Settings,
        tokens: tuple[str | Secret | None, str | Secret | None],
        *,
        name: str,
        server_url: str,
        connect: Connect,
    ) -> None:
        self._settings = settings
        self._token_id = credential("modal", "token_id", tokens[0], "MODAL_TOKEN_ID")
        self._token_secret = credential("modal", "token_secret", tokens[1], "MODAL_TOKEN_SECRET")
        self._server_url = server_url
        self._connect = connect
        self._planes = LoopResources(name, close_plane)
        self.lifetime_ms: int = settings.lifetime_s * 1000
        """The declared provider expiry: Modal ends a sandbox this long after its create."""
        declared = RemoteInfo(name, "unenforced" if settings.internet else "enforced")
        super().__init__(ModalDriver(self._plane), declared)

    async def setup(self) -> None:
        """Resolves both tokens on the host. Channels are opened on first use, in the run."""
        self._resolved()

    def _resolved(self) -> tuple[str, str]:
        try:
            return self._token_id(), self._token_secret()
        except ConfigError as error:
            # One message for the pair: Modal needs both.
            raise ConfigError(
                "missing_secret",
                "modal: set token_id/token_secret or MODAL_TOKEN_ID/MODAL_TOKEN_SECRET",
            ) from error

    def _plane(self) -> Plane:
        def make() -> Plane:
            channels = {self._server_url: self._connect(self._server_url)}
            control = Control(channels[self._server_url], self._settings, self._resolved())
            return Plane(self._connect, control, channels)

        return self._planes.get(make)


def modal(  # noqa: PLR0913 - the options a Modal sandbox is configured by
    *,
    image_id: str,
    token_id: str | Secret | None = None,
    token_secret: str | Secret | None = None,
    app_name: str = "threads",
    environment: str = "",
    lifetime_ms: int = 3_600_000,
    allow_internet: bool = False,
    name: str = "modal",
    server_url: str = SERVER_URL,
    connect: Connect | None = None,
) -> ModalSandbox:
    """A Modal sandbox provider (extra `modal`). `image_id` is a built Modal image (`im-...`,
    for example `modal.Image.debian_slim().build(app)` once at setup); it needs /bin/sh, env
    and tar. The tokens default to `secret("MODAL_TOKEN_ID")` and
    `secret("MODAL_TOKEN_SECRET")`, are resolved at setup and go only to Modal's control plane.
    `name` is SandboxInfo.provider: rows of another name are never touched."""
    if not image_id:
        raise ConfigError("invalid_config", "modal: image_id is required")
    if not _MIN_LIFETIME_MS <= lifetime_ms <= _MAX_LIFETIME_MS:
        raise ConfigError("invalid_config", f"modal: lifetime_ms {lifetime_ms} out of range")
    settings = Settings(
        app_name,
        image_id,
        lifetime_ms // 1000,
        environment,
        allow_internet,
    )
    return ModalSandbox(
        settings,
        (token_id, token_secret),
        name=name,
        server_url=server_url,
        connect=tls_channel if connect is None else connect,
    )
