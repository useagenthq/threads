"""`modal()` (spec/api.json `Sandbox`): Modal sandboxes over the official SDK's generated gRPC
protocol (modal_proto), on grpclib channels this adapter opens and fences.

Why not the high-level `modal.Sandbox`: it runs on synchronicity's own event loop in a thread and
opens its channels internally, so no fence could sit at its send point. The generated stubs on
our own channels give one asyncio loop and a fence at every request's headers.

What Modal 1.5.5 supports here, and nothing broader:
- create is idempotent on the operation key: the sandbox is named `threads-<key>` and Modal
  refuses a second running sandbox of one name (ALREADY_EXISTS). Lookup by that name is
  nonfinal: a create already past its fence may still land.
- the sandbox's declared expiry is `lifetime_ms` (Modal's sandbox timeout).
- terminate is unconfirmed: the task command router has no kill for an exec and no view of its
  descendants. SandboxTerminate(wait) ends the whole sandbox, which is `close`.
- no snapshots: the filesystem snapshot ends the sandbox, can't run during an exec and freezes
  nothing, so it can't be a quiescent capture of a parent that goes on; memory snapshots are
  experimental in the SDK. capture_classes is empty; restore answers snapshot_missing.
- egress is not restricted by this adapter (unenforced).
"""

import os
from typing import Literal

import grpclib.client
from grpclib.const import Status
from grpclib.exceptions import GRPCError

from threads.adapters.sandboxes.fence import dispatch
from threads.adapters.sandboxes.modal.channel import Connect, classify, connect
from threads.adapters.sandboxes.modal.control import Control, Settings
from threads.adapters.sandboxes.modal.router import Router
from threads.adapters.sandboxes.modal.session import ModalSession
from threads.agents.config import ConfigError
from threads.log import SnapshotData
from threads.loop.model import Found, LookupResult, LookupUnknown, NotFoundNonfinal
from threads.result import Err, Ok
from threads.sandbox.protocol import (
    LookupSupport,
    SandboxContext,
    SandboxError,
    SandboxInfo,
    SandboxSession,
)

SERVER_URL = "https://api.modal.com"
_MIN_LIFETIME_MS, _MAX_LIFETIME_MS = 1000, 24 * 3600 * 1000
_CREATE_ERRORS = ("stale_epoch", "cleanup_claim_lost", "timeout")


class ModalSandbox:
    def __init__(self, settings: Settings, *, name: str, server_url: str, connect: Connect) -> None:
        self._settings = settings
        self._server_url = server_url
        self._connect = connect
        self._channels: dict[str, grpclib.client.Channel] = {}
        self._control: Control | None = None
        self.lifetime_ms = settings.lifetime_s * 1000
        """The declared provider expiry: Modal ends a sandbox this long after its create."""
        self._info = SandboxInfo(
            provider=name,
            egress="unenforced",
            capture_classes=(),
            browser="none",
            desktop="none",
            lookup=LookupSupport(create="nonfinal", snapshot="none"),
            termination="unconfirmed",
        )

    @property
    def info(self) -> SandboxInfo:
        return self._info

    async def create(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        control = self._control_plane()

        async def call() -> str:
            try:
                return await control.create(_name(operation_key))
            except GRPCError as error:
                if error.status != Status.ALREADY_EXISTS:
                    raise
                return await control.by_name(_name(operation_key))

        made = await dispatch(context, call, classify)
        if isinstance(made, Err):
            code = made.error.code
            return (
                made
                if code in _CREATE_ERRORS
                else Err(SandboxError("unavailable", str(made.error)))
            )
        return Ok(self._session(made.value))

    async def restore(
        self, snapshot_id: str, manifest_hash: str, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        return Err(SandboxError("snapshot_missing", f"modal: no snapshots here ({snapshot_id})"))

    async def lookup(
        self, operation_key: str, context: SandboxContext
    ) -> LookupResult[SandboxSession]:
        control = self._control_plane()
        found = await dispatch(context, lambda: control.by_name(_name(operation_key)), classify)
        if isinstance(found, Ok):
            return Found(self._session(found.value))
        if found.error.code == "not_found":
            return NotFoundNonfinal()
        return LookupUnknown(f"{found.error.code}: {found.error.message}")

    async def lookup_snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> LookupResult[SnapshotData]:
        return LookupUnknown("modal: this adapter declares no snapshots")

    async def attach(
        self, ref: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        control = self._control_plane()
        running = await dispatch(context, lambda: control.running(ref), classify)
        if isinstance(running, Err):
            return running
        if not running.value:
            return Err(SandboxError("not_found", f"sandbox {ref} has ended"))
        return Ok(self._session(ref))

    async def release(
        self, ref: str, context: SandboxContext
    ) -> Ok[Literal["released", "already_gone"]] | Err[SandboxError]:
        return Err(SandboxError("unavailable", f"modal: no snapshots here ({ref})"))

    async def aclose(self) -> None:
        for channel in self._channels.values():
            channel.close()
        self._channels.clear()

    def _session(self, ident: str) -> ModalSession:
        return ModalSession(ident, self._control_plane(), self._router)

    def _control_plane(self) -> Control:
        if self._control is None:
            self._control = Control(self._channel(self._server_url), self._settings)
        return self._control

    def _router(self, url: str, task_id: str, jwt: str) -> Router:
        return Router(self._channel(url), task_id, jwt)

    def _channel(self, url: str) -> grpclib.client.Channel:
        # One channel per URL, so its fence listener is registered once.
        if url not in self._channels:
            self._channels[url] = self._connect(url)
        return self._channels[url]


def _name(operation_key: str) -> str:
    return f"threads-{operation_key}"


def modal(  # noqa: PLR0913 - the options a Modal sandbox is configured by
    *,
    image_id: str,
    token_id: str | None = None,
    token_secret: str | None = None,
    app_name: str = "threads",
    environment: str = "",
    lifetime_ms: int = 3_600_000,
    name: str = "modal",
    server_url: str = SERVER_URL,
    connect: Connect = connect,
) -> ModalSandbox:
    """A Modal sandbox provider (extra `modal`). `image_id` is a built Modal image (`im-...`,
    for example `modal.Image.debian_slim().build(app)` once at setup); it needs /bin/sh, sed,
    find, sha256sum and stat. Tokens fall back to MODAL_TOKEN_ID / MODAL_TOKEN_SECRET and go
    only to Modal's control plane. `name` is SandboxInfo.provider: rows of another name are
    never touched."""
    token_id = token_id or os.environ.get("MODAL_TOKEN_ID")
    token_secret = token_secret or os.environ.get("MODAL_TOKEN_SECRET")
    if not token_id or not token_secret:
        raise ConfigError("missing_secret", "modal: MODAL_TOKEN_ID and MODAL_TOKEN_SECRET")
    if not image_id:
        raise ConfigError("invalid_config", "modal: image_id is required")
    if not _MIN_LIFETIME_MS <= lifetime_ms <= _MAX_LIFETIME_MS:
        raise ConfigError("invalid_config", f"modal: lifetime_ms {lifetime_ms} out of range")
    settings = Settings(
        token_id, token_secret, app_name, image_id, lifetime_ms // 1000, environment
    )
    return ModalSandbox(settings, name=name, server_url=server_url, connect=connect)
