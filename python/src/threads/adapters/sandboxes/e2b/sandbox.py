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

import os
from typing import Literal

from e2b.api import AsyncApiClient
from e2b.connection_config import ConnectionConfig

from threads.adapters.sandboxes.e2b.control import Control
from threads.adapters.sandboxes.e2b.envd import Envd, Transports
from threads.adapters.sandboxes.e2b.session import E2BSession, Owner, call
from threads.adapters.sandboxes.e2b.transport import FencedHttpx, http_transport, rpc_transport
from threads.adapters.sandboxes.e2b.wire import Sandbox as Described
from threads.agents.config import ConfigError
from threads.log import SnapshotData
from threads.loop.model import Found, LookupResult, LookupUnknown, NotFoundNonfinal
from threads.result import Err, Ok
from threads.sandbox.protocol import (
    LookupSupport,
    SandboxContext,
    SandboxError,
    SandboxId,
    SandboxInfo,
    SandboxSession,
)

HOUR_MS = 3_600_000


class E2BSandbox:
    def __init__(  # noqa: PLR0913 - one provider's settings
        self,
        config: ConnectionConfig,
        transports: Transports,
        *,
        template: str,
        lifetime_ms: int,
        internet: bool,
        name: str,
    ) -> None:
        client = AsyncApiClient(config, transport=FencedHttpx(transports.http))
        self._owner = Owner(name, Control(client))
        self._config = config
        self._transports = transports
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

    async def lookup(
        self, operation_key: str, context: SandboxContext
    ) -> LookupResult[SandboxSession]:
        found = await call(context, lambda: self._owner.control.find(operation_key))
        if isinstance(found, Err):
            return LookupUnknown(f"{found.error.code}: {found.error.message}")
        match found.value:
            case []:
                return NotFoundNonfinal()
            case [ref]:
                attached = await self.attach(ref, context)
                if isinstance(attached, Err):
                    return LookupUnknown(f"{attached.error.code}: {attached.error.message}")
                return Found(attached.value)
            case refs:
                return LookupUnknown(f"several sandboxes carry {operation_key}: {refs}")

    async def lookup_snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> LookupResult[SnapshotData]:
        return LookupUnknown("an E2B snapshot's record can't be rebuilt from a lookup")

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
        domain = described.domain or self._config.domain
        url = self._config.get_sandbox_url(described.sandbox_id, domain)
        envd = Envd(described, url, self._transports)
        return E2BSession(SandboxId(described.sandbox_id), envd, self._owner)


def e2b(  # noqa: PLR0913 - the provider's settings
    *,
    api_key: str | None = None,
    template: str = "base",
    lifetime_ms: int = HOUR_MS,
    allow_internet: bool = False,
    name: str = "e2b",
    domain: str | None = None,
) -> E2BSandbox:
    """An E2B sandbox provider (spec/api.json conventions.adapters). `api_key` falls back to
    E2B_API_KEY; it authenticates the control plane only and never enters a sandbox.
    `template` must carry /bin/sh, sed, find, stat and sha256sum (E2B's base does)."""
    key = api_key or os.environ.get("E2B_API_KEY")
    if not key:
        raise ConfigError("missing_secret", "e2b: set api_key or E2B_API_KEY")
    config = ConnectionConfig(api_key=key, domain=domain, retries=0)
    transports = Transports(http_transport(), rpc_transport())
    return E2BSandbox(
        config,
        transports,
        template=template,
        lifetime_ms=lifetime_ms,
        internet=allow_internet,
        name=name,
    )
