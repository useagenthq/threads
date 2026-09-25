"""`RemoteSandbox`: a provider driver (driver.py) as the `Sandbox` protocol (spec/api.json).
Creation is findable by its operation key, and a restore verifies the restored tree against the
snapshot event's manifest hash before anyone uses it. A provider adapter subclasses it with its
driver and what it declares (`RemoteInfo`); the kit derives the rest of `SandboxInfo` from the
driver, so nothing is declared without its proof."""

from dataclasses import dataclass
from typing import Literal, assert_never

from threads.adapters.sandboxes import posix
from threads.log import SnapshotData
from threads.loop.model import Found, LookupUnknown
from threads.result import Err, Ok
from threads.sandbox.manifest import manifest_hash
from threads.sandbox.protocol import (
    Looked,
    LookupSupport,
    SandboxContext,
    SandboxError,
    SandboxInfo,
    SandboxSession,
    is_refusal,
    unanswered,
)
from threads.sandbox.remote.driver import (
    Confirmed,
    FinalLookup,
    NonfinalLookup,
    SandboxDriver,
    Unconfirmed,
    Unmade,
)
from threads.sandbox.remote.session import RemoteSession, call, released


@dataclass(frozen=True, slots=True)
class RemoteInfo:
    """What a provider declares itself; the kit adds lookup, termination and capture classes."""

    provider: str
    egress: Literal["enforced", "unenforced"]


def _info_of(driver: SandboxDriver, declared: RemoteInfo) -> SandboxInfo:
    capture = driver.capture
    quiescent = capture is not None and capture.quiescence != "unconfirmed"
    return SandboxInfo(
        provider=declared.provider,
        egress=declared.egress,
        capture_classes=("filesystem",) if quiescent else (),
        browser="none",
        desktop="none",
        # There's no snapshot lookup: its event data (manifest hash, quiescence) can't be
        # recovered from the provider.
        lookup=LookupSupport(create=_create_lookup(driver), snapshot="none"),
        termination=_termination(driver),
    )


def _create_lookup(driver: SandboxDriver) -> Literal["nonfinal", "final"]:
    match driver.lookup:
        case FinalLookup():
            return "final"
        case NonfinalLookup():
            return "nonfinal"
        case _:
            assert_never(driver.lookup)


def _termination(driver: SandboxDriver) -> Literal["confirmed", "unconfirmed"]:
    match driver.termination:
        case Confirmed():
            return "confirmed"
        case Unconfirmed():
            return "unconfirmed"
        case _:
            assert_never(driver.termination)


_CREATE_ERRORS = ("stale_epoch", "cleanup_claim_lost", "timeout")


class RemoteSandbox:
    def __init__(self, driver: SandboxDriver, declared: RemoteInfo) -> None:
        self._driver = driver
        self._info = _info_of(driver, declared)

    @property
    def info(self) -> SandboxInfo:
        return self._info

    async def create(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        made = await call(self._driver, context, lambda: self._driver.create(operation_key, None))
        if isinstance(made, Err):
            # Whatever else failed, the create may have happened: the ledger looks it up.
            if made.error.code in _CREATE_ERRORS:
                return made
            return Err(SandboxError("unavailable", made.error.message))
        if isinstance(made.value, Unmade):
            return Err(SandboxError("unavailable", made.value.message))
        return Ok(self._session(made.value))

    async def restore(
        self, snapshot_id: str, manifest_hash: str, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        if self._driver.capture is None:
            return Err(SandboxError("snapshot_missing", f"{self._info.provider}: no snapshots"))
        # A failure here is a lost create response: the ledger resolves it by lookup.
        made = await call(
            self._driver, context, lambda: self._driver.create(operation_key, snapshot_id)
        )
        if isinstance(made, Err):
            return made
        if isinstance(made.value, Unmade):
            return Err(SandboxError(made.value.code, made.value.message))
        return await self._verified(self._session(made.value), manifest_hash, context)

    async def lookup(self, operation_key: str, context: SandboxContext) -> Looked[SandboxSession]:
        find = self._driver.lookup.find
        found = await call(self._driver, context, lambda: find(operation_key))
        if isinstance(found, Err):
            return unanswered(found.error)
        answer = found.value
        return Ok(Found(self._session(answer.value)) if isinstance(answer, Found) else answer)

    async def lookup_snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> Looked[SnapshotData]:
        message = f"{self._info.provider}: a snapshot can't be found by key with its manifest"
        return Ok(LookupUnknown(message))

    async def attach(
        self, ref: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        live = await call(self._driver, context, lambda: self._driver.exists(ref))
        if isinstance(live, Err):
            return live
        if not live.value:
            return Err(SandboxError("not_found", f"no live sandbox {ref}"))
        return Ok(self._session(ref))

    async def release(
        self, ref: str, context: SandboxContext
    ) -> Ok[Literal["released", "already_gone"]] | Err[SandboxError]:
        capture = self._driver.capture
        if capture is None:
            return Err(SandboxError("unavailable", f"{self._info.provider}: no snapshots ({ref})"))
        deleted = await call(self._driver, context, lambda: capture.delete(ref))
        return released(deleted.error) if isinstance(deleted, Err) else deleted

    async def _verified(
        self, child: RemoteSession, expected: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        """The child when its tree hashes to `expected`. Otherwise it is killed before the
        error returns: the ledger reads a typed restore failure as nothing created."""
        tree = await posix.manifest(child, context)
        if isinstance(tree, Err) and is_refusal(tree.error):
            return tree
        if isinstance(tree, Ok) and manifest_hash(tree.value) == expected:
            return Ok(child)
        # Never answered as a lost create (unavailable): the ledger would look the child up and
        # use it unverified. A kill that failed leaves it to the provider's expiry.
        killed = await call(self._driver, context, lambda: self._driver.kill(child.id))
        if isinstance(killed, Err) and is_refusal(killed.error):
            return killed
        if isinstance(tree, Err):
            return Err(SandboxError("snapshot_restore_failed", tree.error.message))
        message = f"the restored tree of {child.id} fails manifest {expected}"
        return Err(SandboxError("snapshot_manifest_mismatch", message))

    def _session(self, ident: str) -> RemoteSession:
        return RemoteSession(self._driver.bound(), self._info.provider, ident)
