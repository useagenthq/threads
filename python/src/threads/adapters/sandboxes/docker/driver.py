"""The remote kit's driver over the Docker Engine API (sandbox/remote/driver.py).

What this adapter declares, and why:
- create: one container per operation key, named `threads-<sha256(key)[:32]>`, labelled with the
  key. A repeated create answers 409 and is that same container. Lookup is by the label and is
  **nonfinal**: the unique name prevents a duplicate, but a negative lookup can't prove that a
  create still in flight won't complete.
- termination **confirmed**: the supervisor's records plus `supervise --terminate` (records.py).
  Nothing is inferred from a scan the guest could forge.
- no snapshots. ponytail: Docker's snapshots are core's host trees (lane 16C), which isn't on
  main; `capture_classes` stays empty until it is, and nothing here declares `capture`.
- egress enforced by `NetworkMode: none`; `allow_internet` puts the container on the default
  bridge, which filters nothing, so egress is then unenforced.

ponytail: two things this adapter leaves to a later lane.
- Orphan gc (16D2 R4) isn't wired into core's gc here. `find_containers` already lists by the
  `threads.operation_key` label, so the sweep is a small addition once gc reaches for it.
- A keyed exec the supervisor refuses admission to exits 125 with `threads: admission refused`
  on stderr, and the caller reads that as the command's exit rather than "nothing ran". The
  daemon reports an exec's exit only after its stream ends, and `run` returns when the exec
  starts, so naming it would cost an extra archive GET before every command. Invariant 2 (one
  executor per branch) makes it rare, and a refused command ran nothing either way.
"""

import errno
from collections.abc import Callable, Mapping, Sequence
from typing import Final, Literal, Self

import httpx
from pydantic import ValidationError

from threads.adapters.sandboxes.docker import archive, create, records, terminate
from threads.adapters.sandboxes.docker import exec as docker_exec
from threads.adapters.sandboxes.docker.create import CreateFailedError, Settings
from threads.adapters.sandboxes.docker.engine import (
    BAD_REQUEST,
    NOT_FOUND,
    Engine,
    EngineError,
    label_of,
)
from threads.adapters.sandboxes.docker.transport import NEEDS_1_44, UNREACHABLE
from threads.adapters.sandboxes.fence import Classify
from threads.adapters.sandboxes.streams import StreamLostError
from threads.log.digest import sha256_hex
from threads.loop.model import Found, LookupUnknown, NotFoundNonfinal
from threads.loop.tools import Termination
from threads.sandbox.protocol import ExecOutput, SandboxError
from threads.sandbox.remote.driver import (
    Capture,
    Confirmed,
    FileError,
    NonfinalLookup,
    Unmade,
)

DEADLINE_CAP_MS: Final = 3_600_000
"""The supervisor's own cap; the adapter clamps to it. The sandbox layer owns the real
deadline (sandbox/exec.py), so a driver run never carries a shorter one."""
COMMAND_UID: Final = "1000"
_UNAVAILABLE = (
    EngineError,
    CreateFailedError,
    archive.ArchiveError,
    docker_exec.FrameError,
    records.SupervisorError,
    ValidationError,
    StreamLostError,
)


def key_hash(process_key: str) -> str:
    """The supervisor's `<key-hash>` argv: lowercase hex, which is what it validates."""
    return sha256_hex(process_key.encode("utf-8"))[:32]


class DockerDriver:
    def __init__(
        self,
        engine: Callable[[], Engine],
        settings: Settings,
        pacing: terminate.Pacing,
        socket: Callable[[], str],
    ) -> None:
        self._engine, self._settings, self._pacing, self._socket = engine, settings, pacing, socket
        self.lookup: NonfinalLookup = NonfinalLookup(self._find)
        self.termination: Confirmed = Confirmed(self._terminate)
        self.capture: Capture | None = None

    def bound(self) -> Self:
        engine = self._engine()
        return type(self)(lambda: engine, self._settings, self._pacing, self._socket)

    @property
    def classify(self) -> Classify:
        return self._classify

    async def create(self, operation_key: str, snapshot: str | None) -> str | Unmade:
        """With no capture the kit never restores, so `snapshot` is always None."""
        return await create.create(self._engine(), operation_key, self._settings)

    async def exists(self, sandbox_id: str) -> bool:
        return await self._engine().inspect_container(sandbox_id) is not None

    async def kill(self, sandbox_id: str) -> Literal["killed", "already_gone"]:
        """The container and all three of its named volumes."""
        engine = self._engine()
        gone = await engine.remove_container(sandbox_id)
        for volume in create.volumes_of(sandbox_id):
            await engine.remove_volume(volume)
        return "killed" if gone else "already_gone"

    async def run(
        self,
        sandbox_id: str,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        process_key: str | None,
    ) -> ExecOutput:
        """A keyed command runs through the supervisor as uid 0, which records it and drops the
        child to uid 1000. The kit's own fixed scripts (tree export and import) carry no key:
        they run as uid 1000 directly and take no lock."""
        if process_key is None:
            spec = docker_exec.body(argv, env, cwd, COMMAND_UID)
            return await docker_exec.start(self._engine(), sandbox_id, spec)
        key = key_hash(process_key)
        cmd = (records.SUPERVISE, key, str(DEADLINE_CAP_MS), *argv)
        spec = docker_exec.body(cmd, env, cwd, "0")
        return await docker_exec.start(self._engine(), sandbox_id, spec, key)

    async def write(self, sandbox_id: str, path: str, data: bytes) -> None:
        """One archive PUT, owned by the command uid: the kit's wrapper (uid 1000) opens the
        stdin file itself and removes the tree archive it extracted, so both must be readable
        and writable by it. That is why stdin goes to the kit's own path under /tmp (a volume,
        create.py) and not to the supervisor's `state/stdin`, which is root-only: the wrapper
        redirects stdin from the path it was given, so `supervise --stdin` could never be
        reached through it."""
        parent, _, name = path.rpartition("/")
        entry = archive.Entry(name, 0o600, 1000, 1000, data)
        try:
            await self._engine().put_archive(sandbox_id, parent or "/", archive.build_tar((entry,)))
        except EngineError as refused:
            if refused.status != NOT_FOUND:
                raise
            await self._made_with_parent(sandbox_id, parent, name, data)

    async def _made_with_parent(self, sandbox_id: str, parent: str, name: str, data: bytes) -> None:
        """The parent doesn't exist yet (the kit's /tmp/threads): one PUT makes both."""
        above, _, directory = parent.rpartition("/")
        tar = archive.build_tar(
            (
                archive.Entry(directory, 0o755, 1000, 1000),
                archive.Entry(f"{directory}/{name}", 0o600, 1000, 1000, data),
            )
        )
        await self._engine().put_archive(sandbox_id, above or "/", tar)

    async def read(self, sandbox_id: str, path: str) -> bytes:
        got = await self._engine().get_archive(sandbox_id, path)
        if got is None:
            raise FileError(SandboxError("not_found", f"no {path} in {sandbox_id}"))
        files = archive.read_tar(got)
        if len(files) != 1:
            raise FileError(SandboxError("is_directory", f"{path} is not one file"))
        return next(iter(files.values()))

    async def _find(self, operation_key: str) -> Found[str] | NotFoundNonfinal | LookupUnknown:
        found = await self._engine().find_containers(label_of(operation_key))
        match found:
            case []:
                return NotFoundNonfinal()
            case [one]:
                return Found(one.names[0].removeprefix("/") if one.names else one.id)
            case many:
                return LookupUnknown(f"{len(many)} containers carry {operation_key}")

    async def _terminate(self, sandbox_id: str, process_key: str) -> Termination:
        return await terminate.terminate(
            self._engine(), sandbox_id, key_hash(process_key), self._pacing
        )

    def _classify(self, error: Exception) -> SandboxError | None:
        """The expected failures of an Engine call; anything else is a bug and raises."""
        if isinstance(error, httpx.TimeoutException):
            return SandboxError("timeout", str(error))
        if isinstance(error, httpx.ConnectError):
            return SandboxError("unavailable", self._unreachable(error))
        if isinstance(error, EngineError) and _is_version_refusal(error):
            return SandboxError("unavailable", NEEDS_1_44)
        if isinstance(error, (*_UNAVAILABLE, httpx.TransportError)):
            return SandboxError("unavailable", str(error))
        return None

    def _unreachable(self, error: httpx.ConnectError) -> str:
        socket = self._socket()
        if socket and _is_denied(error):
            return (
                f"no permission to use {socket}: add your user to the docker group, "
                "or use rootless Docker"
            )
        return UNREACHABLE


def _is_version_refusal(error: EngineError) -> bool:
    """A daemon below the pinned version answers 400 with "client version 1.30 is too old.
    Minimum supported API version is 1.40, ..." (measured on Docker 29.8.0)."""
    return error.status == BAD_REQUEST and "too old" in error.message.lower()


def _is_denied(error: BaseException) -> bool:
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, OSError) and cause.errno in (errno.EACCES, errno.EPERM):
            return True
        cause = cause.__cause__ or cause.__context__
    return False
