"""The sandbox adapter protocol (spec/api.json `Sandbox`, `SandboxSession`, `SandboxInfo`,
`ExecOutput`, `ExecResult`).

Every create and restore carries an operation key that the resource ledger recorded first. Expected
failures are `SandboxError` values; an adapter raises only for bugs.
"""

import posixpath
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, NewType, Protocol, assert_never, runtime_checkable

from threads.log import ArtifactRef, ParseError, SnapshotData
from threads.loop.model import LookupCapability, LookupResult, LookupUnknown
from threads.loop.tools import Termination
from threads.result import Err, Ok
from threads.store.context import CleanupAuthority, OwnerAuthority, SandboxAuthority

SandboxId = NewType("SandboxId", str)

type SandboxErrorCode = Literal[
    "unavailable",
    "timeout",
    "not_found",
    "invalid_path",
    "permission_denied",
    "is_directory",
    "too_large",
    "not_quiescent",
    "release_failed",
    "resource_unknown",
    "snapshot_expired",
    "snapshot_missing",
    "snapshot_restore_failed",
    "snapshot_manifest_mismatch",
    "stale_epoch",
    "cleanup_claim_lost",
]
"""The codes spec/api.json lists for the sandbox methods."""


@dataclass(frozen=True, slots=True)
class WorkspaceRefusal:
    """Why the thread's pinned workspace never reached a fresh sandbox (lane 16 E). Not an
    adapter failure: core places the tree, and the tool call that opened the session sees this
    instead of a bare "never sent"."""

    code: Literal["workspace_mismatch", "capability_missing"]
    message: str


type Refusal = Literal["stale_epoch", "cleanup_claim_lost"]
"""A failed fence: nothing reached the provider, and the caller lost its authority."""

NO_ENV: Final[Mapping[str, str]] = MappingProxyType({})


class SandboxContext(Protocol):
    """What the runtime hands an adapter for every provider operation: who
    dispatches it (an owner's lease, or gc's claim on the row), and `fence`, which the adapter
    awaits at its real dispatch point; a failure means call nothing and answer the `Refusal`
    its authority names."""

    @property
    def authority(self) -> SandboxAuthority: ...

    async def fence(self) -> Ok[None] | Err[ParseError]: ...


@dataclass(frozen=True, slots=True)
class SandboxError:
    code: SandboxErrorCode
    message: str


def refusal(authority: SandboxAuthority) -> Refusal:
    """stale_epoch under an owner's lease, cleanup_claim_lost under gc's claim."""
    match authority:
        case OwnerAuthority():
            return "stale_epoch"
        case CleanupAuthority():
            return "cleanup_claim_lost"
        case _:
            assert_never(authority)


def is_refusal(error: SandboxError) -> bool:
    return error.code in ("stale_epoch", "cleanup_claim_lost")


type Looked[T] = Ok[LookupResult[T]] | Err[SandboxError]
"""spec/api.json `Sandbox.lookup` and `lookupSnapshot` returns: the answer, or a refused fence
(stale_epoch or cleanup_claim_lost), when nothing was asked."""


def unanswered(error: SandboxError) -> Ok[LookupUnknown] | Err[SandboxError]:
    """A lookup whose provider call failed: a refused fence stays an error; any other failure
    is an answer that proves nothing."""
    return Err(error) if is_refusal(error) else Ok(LookupUnknown(f"{error.code}: {error.message}"))


async def refused(context: SandboxContext) -> Err[SandboxError] | None:
    """Checks the fence: None when it passed, else the typed refusal. An adapter calls it only at
    its real provider dispatch point."""
    passed = await context.fence()
    if isinstance(passed, Ok):
        return None
    return Err(SandboxError(refusal(context.authority), passed.error.message))


def invalid_path(path: str) -> Err[SandboxError] | None:
    """A sandbox path is absolute and already normal: no relative parts, no `..` escape."""
    if not path.startswith("/") or posixpath.normpath(path) != path:
        return Err(SandboxError("invalid_path", f"not an absolute normal path: {path}"))
    return None


@dataclass(frozen=True, slots=True)
class LookupSupport:
    """Per operation: create (`Sandbox.lookup`) and snapshot (`Sandbox.lookup_snapshot`)."""

    create: LookupCapability
    snapshot: LookupCapability


@dataclass(frozen=True, slots=True)
class SandboxInfo:
    provider: str
    egress: Literal["enforced", "unenforced"]
    capture_classes: tuple[Literal["filesystem", "filesystem_and_processes", "full_vm"], ...]
    browser: Literal["none", "headless"]
    desktop: Literal["none", "native", "image"]
    lookup: LookupSupport
    termination: Literal["confirmed", "unconfirmed"]
    """unconfirmed: every sandbox_local timeout or crash parks."""


@dataclass(frozen=True, slots=True)
class ExecOutput:
    """What an adapter's exec returns: the complete output as byte streams, never truncated
    or buffered whole. `exit_code` resolves once both streams end."""

    exit_code: Awaitable[int]
    stdout: AsyncIterator[bytes]
    stderr: AsyncIterator[bytes]


@dataclass(frozen=True, slots=True)
class ExecResult:
    """What tools and the log see, built by the sandbox layer from an `ExecOutput`."""

    exit_code: int
    stdout: str
    """Head and tail preview."""
    stderr: str
    """Head and tail preview."""
    truncated: bool
    full_output: ArtifactRef | None = None
    """Present exactly when truncated: the complete output, readable with read_tool_result."""


class SandboxSession(Protocol):
    @property
    def id(self) -> SandboxId: ...

    async def exec(  # noqa: PLR0913 - the options spec/api.json names
        self,
        command: Sequence[str],
        context: SandboxContext,
        *,
        process_key: str,
        cwd: str = "/workspace",
        env: Mapping[str, str] = NO_ENV,
        timeout_ms: int | None = None,
        stdin: bytes | None = None,
    ) -> Ok[ExecOutput] | Err[SandboxError]:
        """Runs with exactly `env`, nothing inherited. `process_key` (the call's effect key) is
        the durable identity of the process group, so recovery can terminate it. A timeout
        kills the whole group."""
        ...

    async def terminate(
        self, process_key: str, context: SandboxContext
    ) -> Ok[Termination] | Err[SandboxError]: ...

    async def upload(
        self, path: str, data: bytes, context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]: ...

    async def download(
        self, path: str, context: SandboxContext
    ) -> Ok[bytes] | Err[SandboxError]: ...

    async def snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SnapshotData] | Err[SandboxError]:
        """Freezes or stops tracked processes, captures, thaws. Returns once durable."""
        ...

    async def close(self, context: SandboxContext) -> Ok[None] | Err[SandboxError]:
        """Releases the sandbox."""
        ...


class Sandbox(Protocol):
    """spec/api.json `Sandbox`. A sandbox whose `info.lookup.create` is not none also
    implements `LooksUpSandbox`, and one whose `info.lookup.snapshot` is not none
    `LooksUpSnapshot`; check() and the first run refuse one that doesn't."""

    @property
    def info(self) -> SandboxInfo: ...

    async def create(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]: ...

    async def restore(
        self, snapshot_id: str, manifest_hash: str, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        """Restores into a new isolated sandbox and verifies it: a restored tree whose
        canonical manifest hash isn't `manifest_hash` is snapshot_manifest_mismatch, and the
        adapter releases the sandbox it created first."""
        ...

    async def attach(
        self, ref: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        """Reattaches to a live row's sandbox by its recorded ref."""
        ...

    async def release(
        self, ref: str, context: SandboxContext
    ) -> Ok[Literal["released", "already_gone"]] | Err[SandboxError]:
        """Releases a snapshot by its durable ref."""
        ...


@runtime_checkable
class Trees(Protocol):
    """spec/api.json `SandboxSession.export_tree` and `import_tree`, an optional capability of a
    `SandboxSession`: /workspace moved as one tar archive. Core reads every export with the
    strict tree reader (sandbox/trees.py)."""

    async def export_tree(self, context: SandboxContext) -> Ok[ExecOutput] | Err[SandboxError]:
        """The archive on stdout, then exit code 0; any other code is a failed export."""
        ...

    async def import_tree(
        self, tar: AsyncIterable[bytes], context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]:
        """Extracts a host-built archive (modes already masked to 0o777) into /workspace, owner
        dropped."""
        ...


@runtime_checkable
class LooksUpSandbox(Protocol):
    """spec/api.json `Sandbox.lookup`, an optional capability of a `Sandbox`."""

    async def lookup(self, operation_key: str, context: SandboxContext) -> Looked[SandboxSession]:
        """Finds a sandbox whose create or restore answer was lost."""
        ...


@runtime_checkable
class LooksUpSnapshot(Protocol):
    """spec/api.json `Sandbox.lookupSnapshot`, an optional capability of a `Sandbox`."""

    async def lookup_snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> Looked[SnapshotData]:
        """Finds a snapshot whose capture answer was lost."""
        ...
