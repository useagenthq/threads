"""`e2b()`: the E2B sandbox provider (spec/api.json `Sandbox`) over the official `e2b` SDK.

What it declares, and why:
- create: tagged with its operation key in sandbox metadata, found again by a metadata query.
  A create that passed its fence may still be in flight when the query runs, so a miss is
  never final (lookup.create nonfinal).
- snapshots: E2B's snapshot pauses the whole VM and captures memory and disk (full_vm); it
  lives until deleted (expires_at null). Refused while envd runs a process threads started.
  Its record carries no manifest hash, so a lost snapshot answer can't be rebuilt from a
  lookup (lookup.snapshot none: the row parks for an operator).
- restore creates a sandbox from the snapshot and verifies the /workspace manifest; on a
  mismatch it kills the child first.
- termination unconfirmed: envd kills the process it started, not what that process detached.
- egress enforced only with the internet off (the default); on, it is unenforced.
- the sandbox dies on its own `lifetime_ms` after create (E2B's timeout): the declared expiry.
"""

import os
from typing import Literal

from e2b.api import AsyncApiClient
from e2b.connection_config import ConnectionConfig

from threads.adapters.sandboxes import posix
from threads.adapters.sandboxes.e2b.control import Control
from threads.adapters.sandboxes.e2b.envd import Envd, Transports
from threads.adapters.sandboxes.e2b.session import E2BSession, Owner, call
from threads.adapters.sandboxes.e2b.transport import FencedHttpx, http_transport, rpc_transport
from threads.adapters.sandboxes.e2b.wire import Sandbox as Described
from threads.agents.config import ConfigError
from threads.log import SnapshotData
from threads.loop.model import Found, LookupResult, LookupUnknown, NotFoundNonfinal
from threads.result import Err, Ok
from threads.sandbox.manifest import manifest_hash as hash_of
from threads.sandbox.protocol import (
    LookupSupport,
    SandboxContext,
    SandboxError,
    SandboxId,
    SandboxInfo,
    SandboxSession,
    is_refusal,
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
            capture_classes=("full_vm",),
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
        made = await self._create(snapshot_id, operation_key, context)
        if isinstance(made, Err):
            timed_out = made.error.code == "timeout"
            return Err(SandboxError("unavailable", made.error.message) if timed_out else made.error)
        if made.value is None:
            return Err(SandboxError("snapshot_missing", f"E2B has no snapshot {snapshot_id}"))
        return await verified(made.value, manifest_hash, context)

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
        deleted = await call(context, lambda: self._owner.control.delete_snapshot(ref))
        if isinstance(deleted, Err):
            if is_refusal(deleted.error):
                return deleted
            return Err(SandboxError("release_failed", deleted.error.message))
        return Ok("released" if deleted.value else "already_gone")

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


async def verified(
    child: E2BSession, manifest_hash: str, context: SandboxContext
) -> Ok[SandboxSession] | Err[SandboxError]:
    """The restored child, once its /workspace manifest hashes to `manifest_hash`. On a
    mismatch the child is killed first; if that kill fails, the answer is unavailable so the
    ledger finds the child by its key instead of calling it released."""
    tree = await posix.manifest(child, context)
    if isinstance(tree, Ok) and hash_of(tree.value) == manifest_hash:
        return Ok(child)
    if isinstance(tree, Err) and is_refusal(tree.error):
        return tree
    closed = await child.close(context)
    if isinstance(closed, Err):
        refused = is_refusal(closed.error)
        return closed if refused else Err(SandboxError("unavailable", closed.error.message))
    if isinstance(tree, Err):
        return Err(SandboxError("snapshot_restore_failed", tree.error.message))
    return Err(SandboxError("snapshot_manifest_mismatch", f"{child.id}: the restored tree differs"))


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
