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
- The manifest a snapshot returns is the parent's just before the stop: a claim. Core proves
  it against the image by a ledgered restore before recording the snapshot, and refuses one
  whose image differs (thread/snapshot.py).
"""

import asyncio
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

import aiohttp

from threads.adapters.sandboxes import posix
from threads.adapters.sandboxes.daytona import transport
from threads.adapters.sandboxes.daytona.control import Control, Placement, classify, client
from threads.adapters.sandboxes.daytona.session import DaytonaSession, resource_name
from threads.adapters.sandboxes.daytona.toolbox import Toolbox
from threads.adapters.sandboxes.daytona.wire import SandboxDto
from threads.adapters.sandboxes.fence import dispatch
from threads.agents.config import ConfigError
from threads.log import SnapshotData
from threads.loop.model import Found, LookupResult, LookupUnknown, NotFoundNonfinal
from threads.result import Err, Ok
from threads.sandbox import manifest
from threads.sandbox.protocol import (
    LookupSupport,
    SandboxContext,
    SandboxError,
    SandboxInfo,
    SandboxSession,
)
from threads.secrets import Secret, credential

if TYPE_CHECKING:
    from threads.sandbox.manifest import ManifestEntry

DEFAULT_API = "https://app.daytona.io/api"
API_KEY = "DAYTONA_API_KEY"
_PROVIDER = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PUMP_GRACE_S = 0.25


class DaytonaSandbox:
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
        self.lifetime_ms: int | None = lifetime_ms
        """The declared provider expiry of a created sandbox, when set."""
        ttl = None if lifetime_ms is None else -(-lifetime_ms // 60_000)
        self._placed = Placement(
            target, ttl, auto_stop_minutes=auto_stop_minutes, block_network=not allow_internet
        )
        self._key = credential("daytona", "api_key", api_key, API_KEY)
        self._api_url, self._base = api_url, snapshot
        self._name, self._poll_s, self._wait_s, self._traces = name, poll_s, wait_s, traces
        self._session: aiohttp.ClientSession | None = None
        self._control: Control | None = None
        self._pumps: set[asyncio.Task[None]] = set()

    @property
    def info(self) -> SandboxInfo:
        return SandboxInfo(
            provider=self._name,
            egress="unenforced" if not self._placed.block_network else "enforced",
            capture_classes=("filesystem",),
            browser="none",
            desktop="none",
            lookup=LookupSupport(create="nonfinal", snapshot="none"),
            termination="unconfirmed",
        )

    async def setup(self) -> None:
        """Resolves the key on the host. The HTTP session is opened on first use, in the run
        and on its event loop."""
        self._key()

    async def aclose(self) -> None:
        """Closes the session, then the exec streams still reading it. The session goes first:
        closing it closes every connection it holds or is opening, so a pump mid-request ends
        with an error rather than being cancelled in a connect that leaves a socket behind."""
        if self._session is not None:
            await self._session.close()
        if self._pumps:
            _, stuck = await asyncio.wait(self._pumps, timeout=_PUMP_GRACE_S)
            for pump in stuck:  # waiting on a reader that is gone, not on the network
                pump.cancel()
            await asyncio.gather(*stuck, return_exceptions=True)

    async def create(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        name = resource_name(operation_key)
        return await dispatch(
            context, lambda: self._create(name, self._base, self._placed), classify
        )

    async def restore(
        self, snapshot_id: str, manifest_hash: str, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        name = resource_name(operation_key)
        made = await self._measured(snapshot_id, name, self._placed, context)
        if isinstance(made, Err):
            return made
        child, tree = made.value
        if manifest.manifest_hash(tree) == manifest_hash:
            return Ok(child)
        await child.close(context)
        return Err(SandboxError("snapshot_manifest_mismatch", f"{snapshot_id}: manifest"))

    async def lookup(
        self, operation_key: str, context: SandboxContext
    ) -> LookupResult[SandboxSession]:
        found = await dispatch(context, lambda: self._find(resource_name(operation_key)), classify)
        if isinstance(found, Err):
            return LookupUnknown(f"{found.error.code}: {found.error.message}")
        return NotFoundNonfinal() if found.value is None else Found(found.value)

    async def lookup_snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> LookupResult[SnapshotData]:
        return LookupUnknown("a Daytona snapshot can't be found by key with its manifest")

    async def attach(
        self, ref: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        found = await dispatch(context, lambda: self._find(ref), classify)
        if isinstance(found, Err):
            return found
        if found.value is None:
            return Err(SandboxError("not_found", f"no sandbox {ref}"))
        return Ok(found.value)

    async def release(
        self, ref: str, context: SandboxContext
    ) -> Ok[Literal["released", "already_gone"]] | Err[SandboxError]:
        removed = await dispatch(context, lambda: self._controls().remove(ref), classify)
        if isinstance(removed, Err):
            if removed.error.code != "unavailable":
                return removed
            return Err(SandboxError("release_failed", removed.error.message))
        return Ok("released" if removed.value else "already_gone")

    async def _measured(
        self, snapshot_id: str, name: str, placed: Placement, context: SandboxContext
    ) -> "Ok[tuple[DaytonaSession, list[ManifestEntry]]] | Err[SandboxError]":
        """A sandbox restored from the snapshot, and its manifest; deleted if unmeasurable."""
        made = await dispatch(context, lambda: self._restore(snapshot_id, name, placed), classify)
        if isinstance(made, Err):
            return made
        if isinstance(made.value, SandboxError):
            return Err(made.value)
        child = made.value
        tree = await posix.manifest(child, context)
        if isinstance(tree, Err):
            await child.close(context)
            return Err(SandboxError("snapshot_restore_failed", tree.error.message))
        return Ok((child, tree.value))

    async def _create(self, name: str, snapshot: str | None, placed: Placement) -> DaytonaSession:
        dto = await self._controls().create(name, snapshot, placed)
        await self._toolbox(dto).prepare()
        return self._open(dto)

    async def _restore(
        self, snapshot: str, name: str, placed: Placement
    ) -> DaytonaSession | SandboxError:
        snap = await self._controls().snapshot(snapshot)
        if snap is None:
            return SandboxError("snapshot_missing", f"no snapshot {snapshot}")
        if snap.state == "inactive":
            return SandboxError("snapshot_expired", f"snapshot {snapshot} is inactive")
        if snap.state != "active":
            return SandboxError("snapshot_restore_failed", f"snapshot {snapshot} is {snap.state}")
        return await self._create(name, snap.name, placed)

    async def _find(self, ref: str) -> DaytonaSession | None:
        found = await self._controls().get(ref)
        return None if found is None else self._open(found)

    def _open(self, dto: SandboxDto) -> DaytonaSession:
        return DaytonaSession(dto.id, self._name, self._controls(), self._toolbox(dto))

    def _toolbox(self, dto: SandboxDto) -> Toolbox:
        return Toolbox(
            dto.toolbox_proxy_url,
            dto.id,
            session=self._http(),
            poll_s=self._poll_s,
            wait_s=self._wait_s,
            tasks=self._pumps,
        )

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = transport.session(self._key(), self._traces)
        return self._session

    def _controls(self) -> Control:
        if self._control is None:
            api = client(self._api_url, self._http())
            self._control = Control(api, self._poll_s, self._wait_s)
        return self._control


def daytona(  # noqa: PLR0913 - the provider's options
    *,
    api_key: str | Secret | None = None,
    api_url: str = DEFAULT_API,
    snapshot: str | None = None,
    target: str | None = None,
    lifetime_ms: int | None = None,
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
