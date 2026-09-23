"""The sandbox adapter protocol (spec/api.json `Sandbox`, `SandboxSession`, `SandboxInfo`,
`ExecOutput`, `ExecResult`; ).

Every create and restore carries an operation key that the resource ledger recorded first
. Expected failures are `SandboxError` values; an adapter raises only for bugs.
"""

from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, NewType, Protocol

from threads.log import ArtifactRef, SnapshotData
from threads.loop.model import LookupCapability, LookupResult
from threads.loop.tools import Termination
from threads.result import Err, Ok

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
]
"""The codes spec/api.json lists for the sandbox methods."""

NO_ENV: Final[Mapping[str, str]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class SandboxError:
    code: SandboxErrorCode
    message: str


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

    async def terminate(self, process_key: str) -> Ok[Termination] | Err[SandboxError]: ...

    async def upload(self, path: str, data: bytes) -> Ok[None] | Err[SandboxError]: ...

    async def download(self, path: str) -> Ok[bytes] | Err[SandboxError]: ...

    async def snapshot(self, operation_key: str) -> Ok[SnapshotData] | Err[SandboxError]:
        """Freezes or stops tracked processes, captures, thaws. Returns once durable."""
        ...

    async def close(self) -> Ok[None] | Err[SandboxError]:
        """Releases the sandbox."""
        ...


class Sandbox(Protocol):
    @property
    def info(self) -> SandboxInfo: ...

    async def create(self, operation_key: str) -> Ok[SandboxSession] | Err[SandboxError]: ...

    async def restore(
        self, snapshot_id: str, operation_key: str
    ) -> Ok[SandboxSession] | Err[SandboxError]: ...

    async def lookup(self, operation_key: str) -> LookupResult[SandboxSession]:
        """Finds a sandbox whose create or restore answer was lost. Answers only when
        `info.lookup.create` is not none."""
        ...

    async def lookup_snapshot(self, operation_key: str) -> LookupResult[SnapshotData]: ...

    async def attach(self, ref: str) -> Ok[SandboxSession] | Err[SandboxError]:
        """Reattaches to a live row's sandbox by its recorded ref."""
        ...

    async def release(
        self, ref: str
    ) -> Ok[Literal["released", "already_gone"]] | Err[SandboxError]:
        """Releases a snapshot by its durable ref."""
        ...
