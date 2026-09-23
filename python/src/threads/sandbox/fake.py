"""`fake_sandbox` (spec/api.json `fakeSandbox`): an in-memory provider for tests, driven by a
conformance SandboxScript. Snapshots copy the file tree; a scripted snapshot restores into its
`restore_sandbox_id`, and may lose the restore's answer after creating the sandbox."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, NotRequired, TypedDict

from pydantic import ConfigDict, JsonValue, TypeAdapter, with_config

from threads.log import SnapshotData
from threads.loop.model import Found, LookupResult, LookupUnknown, NotFound
from threads.result import Err, Ok
from threads.sandbox.fake_session import FakeSession, ToolScript
from threads.sandbox.manifest import ManifestEntry, manifest_of
from threads.sandbox.manifest import manifest_hash as tree_hash
from threads.sandbox.protocol import (
    LookupSupport,
    SandboxContext,
    SandboxError,
    SandboxId,
    SandboxInfo,
    SandboxSession,
    refused,
)


@with_config(ConfigDict(extra="forbid", strict=True))
class SnapshotScript(TypedDict):
    restore_sandbox_id: str
    manifest: list[ManifestEntry]
    restore_response: NotRequired[Literal["ok", "lost", "crash"]]
    create_lookup: NotRequired[Literal["found", "unsupported"]]


@with_config(ConfigDict(extra="forbid", strict=True))
class SandboxScript(TypedDict):
    tools: NotRequired[dict[str, ToolScript]]
    snapshots: NotRequired[dict[str, SnapshotScript]]


_SCRIPT: TypeAdapter[SandboxScript] = TypeAdapter(SandboxScript)


class FakeCrashError(Exception):
    """Test kit: the host dies right after a restore succeeds (restore_response: crash)."""


@dataclass(frozen=True, slots=True)
class _Snapshot:
    files: Mapping[str, bytes]
    manifest: list[ManifestEntry]
    script: SnapshotScript | None = None


@dataclass
class FakeSandbox:
    """spec/api.json `Sandbox`, in memory. `creates` counts provider create and restore calls;
    `fail_releases` makes that many releases fail first (a test hook for release_failed)."""

    script: SandboxScript
    creates: int = 0
    fail_releases: int = 0
    releases: int = 0
    """Release and close calls that passed their fence and reached the provider."""
    provider: str = "fake"
    _live: dict[str, FakeSession] = field(default_factory=dict[str, FakeSession])
    _snapshots: dict[str, _Snapshot] = field(default_factory=dict[str, _Snapshot])
    _by_key: dict[str, SandboxSession | SnapshotData] = field(
        default_factory=dict[str, SandboxSession | SnapshotData]
    )
    _next: int = 0

    def __post_init__(self) -> None:
        for name, snap in self.script.get("snapshots", {}).items():
            self._snapshots[name] = _Snapshot({}, snap["manifest"], snap)

    @property
    def info(self) -> SandboxInfo:
        scripted = self.script.get("snapshots", {}).values()
        lookup = (
            "none" if any(s.get("create_lookup") == "unsupported" for s in scripted) else "final"
        )
        return SandboxInfo(
            provider=self.provider,
            egress="enforced",
            capture_classes=("filesystem",),
            browser="none",
            desktop="none",
            lookup=LookupSupport(create=lookup, snapshot="final"),
            termination="confirmed",
        )

    async def create(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        stale = await refused(context)
        if stale is not None:
            return stale
        self.creates += 1
        return Ok(self._open(SandboxId(self._name("sbx")), {}, operation_key))

    async def restore(
        self, snapshot_id: str, manifest_hash: str, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        snap = self._snapshots.get(snapshot_id)
        if snap is None:
            return Err(SandboxError("snapshot_missing", f"no snapshot {snapshot_id}"))
        stale = await refused(context)
        if stale is not None:
            return stale
        self.creates += 1
        script = snap.script or SnapshotScript(restore_sandbox_id=self._name("sbx"), manifest=[])
        session = self._open(SandboxId(script["restore_sandbox_id"]), snap.files, operation_key)
        if tree_hash(snap.manifest) != manifest_hash:
            # The adapter releases what it created before reporting the mismatch.
            self._live.pop(session.id, None)
            self._by_key.pop(operation_key, None)
            return Err(SandboxError("snapshot_manifest_mismatch", f"{snapshot_id}: manifest"))
        match script.get("restore_response"):
            case "lost":
                return Err(SandboxError("unavailable", "the restore's answer was lost"))
            case "crash":
                raise FakeCrashError(f"the host died after restoring {snapshot_id}")
            case "ok" | None:
                return Ok(session)

    async def lookup(
        self, operation_key: str, context: SandboxContext
    ) -> LookupResult[SandboxSession]:
        if await refused(context) is not None:
            return LookupUnknown("stale_epoch: the owner lost its lease")
        found = self._by_key.get(operation_key)
        return NotFound() if isinstance(found, SnapshotData | None) else Found(found)

    async def lookup_snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> LookupResult[SnapshotData]:
        if await refused(context) is not None:
            return LookupUnknown("stale_epoch: the owner lost its lease")
        found = self._by_key.get(operation_key)
        return Found(found) if isinstance(found, SnapshotData) else NotFound()

    async def attach(
        self, ref: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        stale = await refused(context)
        if stale is not None:
            return stale
        session = self._live.get(ref)
        return (
            Err(SandboxError("not_found", f"no sandbox {ref}")) if session is None else Ok(session)
        )

    async def release(
        self, ref: str, context: SandboxContext
    ) -> Ok[Literal["released", "already_gone"]] | Err[SandboxError]:
        stale = await refused(context)
        if stale is not None:
            return stale
        self.releases += 1
        failed = self._failed_release()
        if failed is not None:
            return failed
        return Ok("released" if self._snapshots.pop(ref, None) is not None else "already_gone")

    def _open(self, ident: SandboxId, files: Mapping[str, bytes], key: str) -> FakeSession:
        tools = self.script.get("tools", {})
        session = FakeSession(ident, files, tools, self._capture, self._close)
        self._live[ident] = session
        self._by_key[key] = session
        return session

    def _capture(self, session: FakeSession, key: str) -> SnapshotData:
        name = self._name("snap")
        manifest = manifest_of(session.files)
        self._snapshots[name] = _Snapshot(dict(session.files), manifest)
        data: dict[str, JsonValue] = {
            "snapshot_id": name,
            "provider": self.provider,
            "sandbox_id": session.id,
            "capture_class": "filesystem",
            "expires_at": None,
            "manifest_hash": tree_hash(manifest),
            "quiesced": {"frozen": [], "stopped": [], "excluded": []},
        }
        snapshot = SnapshotData.model_validate(data)
        self._by_key[key] = snapshot
        return snapshot

    def _close(self, session: FakeSession) -> Ok[None] | Err[SandboxError]:
        self.releases += 1
        failed = self._failed_release()
        if failed is not None:
            return failed
        self._live.pop(session.id, None)
        return Ok(None)

    def _failed_release(self) -> Err[SandboxError] | None:
        if self.fail_releases == 0:
            return None
        self.fail_releases -= 1
        return Err(SandboxError("release_failed", "the provider failed to release"))

    def _name(self, prefix: str) -> str:
        self._next += 1
        return f"{prefix}_{self._next}"


def fake_sandbox(script: Mapping[str, JsonValue] | None = None) -> FakeSandbox:
    """spec/api.json `fakeSandbox`. Raises on a malformed script: it is test-kit config."""
    return FakeSandbox(_SCRIPT.validate_python(script or {}))
