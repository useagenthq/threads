"""What a provider adapter implements to become a threads `Sandbox` through `RemoteSandbox`
(sandbox.py): the provider's raw operations over its fenced transport. Every call runs inside
`fence.dispatch`, so each request it sends passes the lease fence at its real send point.
A transport or provider failure raises; the driver's `classify` names the expected ones.

Termination and quiescence are never inferred from inside the guest: only a provider
control-plane primitive (a whole-sandbox pause, stop or kill) proves them. So a driver declares
what it can prove, and each stronger declaration carries its proof:
- `FinalLookup`: a `find` whose absence is final. Without it, a lookup never proves absence.
- `Confirmed`: a `terminate` that answers terminated, already_exited or unknown. Without it,
  `stop_process` is best effort and every termination is unknown.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from threads.adapters.sandboxes.fence import Classify
from threads.loop.model import Found, LookupResult, LookupUnknown, NotFoundNonfinal
from threads.loop.tools import Termination
from threads.sandbox.protocol import ExecOutput, SandboxError

type Quiescence = Literal["paused", "stopped", "unconfirmed"]
"""How the provider makes a capture quiescent, declared from its documentation, never broader:
- paused: a provider-owned pause of the whole sandbox freezes every process for the capture.
- stopped: the provider stops the whole sandbox for it; its processes end, so a snapshot is
  disruptive to the parent.
- unconfirmed: no documented whole-sandbox boundary; the snapshot capability is absent."""


class FileError(Exception):
    """The provider refused a file operation for a reason it names (not_found, is_directory,
    permission_denied, too_large, invalid_path)."""

    def __init__(self, error: SandboxError) -> None:
        super().__init__(error.message)
        self.error = error


@dataclass(frozen=True, slots=True)
class Unmade:
    """A create from a snapshot that the provider refused before making anything."""

    code: Literal["snapshot_missing", "snapshot_expired", "snapshot_restore_failed"]
    message: str


@dataclass(frozen=True, slots=True)
class Taken:
    ref: str
    expires_at: int | None


@dataclass(frozen=True, slots=True)
class Capture:
    quiescence: Quiescence
    take: Callable[[str, str], Awaitable[Taken]]
    """(sandbox id, operation key): captures the filesystem into a durable snapshot named by
    the key behind the declared boundary, and returns with the sandbox running again."""
    delete: Callable[[str], Awaitable[Literal["released", "already_gone"]]]
    """Deletes a snapshot by its ref."""


@dataclass(frozen=True, slots=True)
class NonfinalLookup:
    """A create in flight at the provider may appear later, so absence is never proof."""

    find: Callable[[str], Awaitable[Found[str] | NotFoundNonfinal | LookupUnknown]]


@dataclass(frozen=True, slots=True)
class FinalLookup:
    """The provider's contract proves a key it doesn't find was never created."""

    find: Callable[[str], Awaitable[LookupResult[str]]]


@dataclass(frozen=True, slots=True)
class Unconfirmed:
    stop_process: Callable[[str, str], Awaitable[None]]
    """(sandbox id, process key): kills the process the provider recorded under the key. It
    proves nothing: a descendant can outlive it."""


@dataclass(frozen=True, slots=True)
class Confirmed:
    terminate: Callable[[str, str], Awaitable[Termination]]
    """(sandbox id, process key): ends the key's whole process group and says whether it did."""


class SandboxDriver(Protocol):
    @property
    def classify(self) -> Classify:
        """The provider's expected failures; None re-raises (a bug)."""
        ...

    @property
    def lookup(self) -> NonfinalLookup | FinalLookup: ...

    @property
    def termination(self) -> Unconfirmed | Confirmed: ...

    @property
    def capture(self) -> Capture | None:
        """None when the provider has no snapshots."""
        ...

    async def create(self, operation_key: str, snapshot: str | None) -> str | Unmade:
        """The new sandbox's id, made so that `find(operation_key)` locates it (a tag, label
        or unique name), from the snapshot `snapshot` when given, with /workspace ready.
        Nothing from the host env is passed into it."""
        ...

    async def exists(self, sandbox_id: str) -> bool: ...

    async def kill(self, sandbox_id: str) -> Literal["killed", "already_gone"]: ...

    async def run(
        self,
        sandbox_id: str,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        process_key: str | None,
    ) -> ExecOutput:
        """Starts `argv` with `env` through the provider API (never its argv), recorded by the
        provider under `process_key` when given. Returns once the provider accepted it."""
        ...

    async def write(self, sandbox_id: str, path: str, data: bytes) -> None:
        """Raises FileError for a refusal the provider names."""
        ...

    async def read(self, sandbox_id: str, path: str) -> bytes:
        """Raises FileError for a refusal the provider names."""
        ...
