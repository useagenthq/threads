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
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from e2b.api import AsyncApiClient
from e2b.connection_config import ConnectionConfig

from threads.adapters.loop_resources import LoopResources, close_all
from threads.adapters.sandboxes.e2b.control import Control
from threads.adapters.sandboxes.e2b.envd import Envd, Transports
from threads.adapters.sandboxes.e2b.session import E2BSession, Owner, call
from threads.adapters.sandboxes.e2b.transport import FencedHttpx, http_transport, rpc_transport
from threads.adapters.sandboxes.e2b.wire import Sandbox as Described
from threads.log import SnapshotData
from threads.loop.model import Found, LookupUnknown, NotFoundNonfinal
from threads.result import Err, Ok
from threads.sandbox.protocol import (
    Looked,
    LookupSupport,
    SandboxContext,
    SandboxError,
    SandboxId,
    SandboxInfo,
    SandboxSession,
    unanswered,
)
from threads.secrets import Secret, credential

HOUR_MS = 3_600_000
API_KEY = "E2B_API_KEY"


class E2BSandbox:
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
        self._name = name
        self._planes = LoopResources(name, _close)
        self._transports = transports
        """Makes one event loop's transports."""
        self._template = template
        self._internet = internet
        self.lifetime_ms = lifetime_ms
        """The declared provider expiry: E2B kills the sandbox this long after create."""
        self._info = SandboxInfo(
            provider=name,
            egress="unenforced" if internet else "enforced",
            capture_classes=(),
            browser="none",
            desktop="none",
            lookup=LookupSupport(create="nonfinal", snapshot="none"),
            termination="unconfirmed",
        )

    @property
    def info(self) -> SandboxInfo:
        return self._info

    async def setup(self) -> None:
        """Resolves the key on the host. The clients are made on first use, in the run and on
        its event loop, and closed when nothing holds that loop any more."""
        self._key()

    def _plane(self) -> "_Plane":
        def make() -> _Plane:
            config = ConnectionConfig(
                api_key=self._key(), domain=self._domain, api_url=self._api_url, retries=0
            )
            transports = self._transports()
            client = AsyncApiClient(config, transport=FencedHttpx(transports.http))
            return _Plane(transports, config, client, Owner(self._name, Control(client)))

        return self._planes.get(make)

    @property
    def _owner(self) -> Owner:
        return self._plane().owner

    async def create(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        made = await self._create(self._template, operation_key, context)
        if isinstance(made, Err):
            return made
        if made.value is None:
            return Err(SandboxError("unavailable", f"E2B has no template {self._template}"))
        return Ok(made.value)

    async def restore(
        self, snapshot_id: str, manifest_hash: str, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        return Err(SandboxError("snapshot_missing", "e2b: this adapter takes no snapshots"))

    async def lookup(self, operation_key: str, context: SandboxContext) -> Looked[SandboxSession]:
        found = await call(context, lambda: self._owner.control.find(operation_key))
        if isinstance(found, Err):
            return unanswered(found.error)
        match found.value:
            case []:
                return Ok(NotFoundNonfinal())
            case [ref]:
                attached = await self.attach(ref, context)
                if isinstance(attached, Err):
                    return unanswered(attached.error)
                return Ok(Found(attached.value))
            case refs:
                return Ok(LookupUnknown(f"several sandboxes carry {operation_key}: {refs}"))

    async def lookup_snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> Looked[SnapshotData]:
        return Ok(LookupUnknown("an E2B snapshot's record can't be rebuilt from a lookup"))

    async def attach(
        self, ref: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        described = await call(context, lambda: self._owner.control.describe(ref))
        if isinstance(described, Err):
            return described
        if described.value is None:
            return Err(SandboxError("not_found", f"E2B has no sandbox {ref}"))
        return Ok(self._session(described.value))

    async def release(
        self, ref: str, context: SandboxContext
    ) -> Ok[Literal["released", "already_gone"]] | Err[SandboxError]:
        return Err(SandboxError("unavailable", f"e2b: this adapter takes no snapshots ({ref})"))

    async def _create(
        self, template: str, key: str, context: SandboxContext
    ) -> Ok[E2BSession | None] | Err[SandboxError]:
        timeout_s = max(1, self.lifetime_ms // 1000)

        async def create() -> Described | None:
            return await self._owner.control.create(
                template, key, timeout_s=timeout_s, internet=self._internet
            )

        made = await call(context, create)
        if isinstance(made, Err):
            return made
        return Ok(None if made.value is None else self._session(made.value))

    def _session(self, described: Described) -> E2BSession:
        plane = self._plane()
        domain = described.domain or plane.config.domain
        url = plane.config.get_sandbox_url(described.sandbox_id, domain)
        envd = Envd(described, url, plane.transports, plane.envds)
        return E2BSession(SandboxId(described.sandbox_id), envd, plane.owner)


@dataclass(frozen=True, slots=True)
class _Plane:
    """One event loop's E2B clients: the transports, the control client over them, and every
    sandbox's envd clients opened on that loop."""

    transports: Transports
    config: ConnectionConfig
    client: AsyncApiClient
    owner: Owner
    envds: set[Envd] = field(default_factory=set[Envd])


async def _close(plane: _Plane) -> None:
    """Every envd (its exec streams, then its clients), the control client, then the
    transports they all share; each is attempted even if one before it fails."""
    envds = [envd.aclose for envd in list(plane.envds)]
    control = plane.client.get_async_httpx_client().aclose
    await close_all([*envds, control, plane.transports.aclose])


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
    `template` must carry /bin/sh, sed, find, stat and sha256sum (E2B's base does)."""
    return E2BSandbox(
        api_key,
        lambda: Transports(http_transport(), rpc_transport()),
        domain=domain,
        template=template,
        lifetime_ms=lifetime_ms,
        internet=allow_internet,
        name=name,
    )
