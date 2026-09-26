"""The create sequence: one container per operation key, with the supervisor injected.

Every step is one fenced request. The order matters: the container is made stopped, its applied
limits are checked against what was asked (a limit the daemon silently dropped fails the create
rather than running unlimited), the supervisor archive is PUT into the exec volume while nothing
is running, and only then does the container start.

Invariant 4: the request adds nothing to the environment but PATH, no registry credentials are
ever sent, and the injected binary must hash to its pin.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import JsonValue, ValidationError

from threads.adapters.sandboxes.docker import archive
from threads.adapters.sandboxes.docker import exec as docker_exec
from threads.adapters.sandboxes.docker.engine import (
    NOT_FOUND,
    Engine,
    EngineError,
    NameTakenError,
)
from threads.adapters.sandboxes.docker.records import SUPERVISE
from threads.adapters.sandboxes.docker.wire import Tools
from threads.agents.config import ConfigError
from threads.log.digest import sha256_hex

ROOT: Final = "/run/threads"
WORKSPACE: Final = "/workspace"
TMP: Final = "/tmp"  # noqa: S108 - a mount inside the container, not on the host
PIDS_LIMIT: Final = 1024
PATH_ENV: Final = "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ARCHES: Final[Mapping[str, str]] = {"amd64": "linux-amd64", "arm64": "linux-arm64"}
BINARIES: Final[Mapping[str, str]] = {
    # docker/supervise/binaries.json, written by scripts/build-supervisor.sh. Each binary is
    # hashed here before it is injected; a mismatch is refused (tests pin these two values).
    "linux-amd64": "be6997cac935a56bae4ccecc0efb858a145d6349e7a6745645e681be0df55491",
    "linux-arm64": "0bc5ab34f374decebad166ddd039796744c19c601fee0478a108dd5e4907ea95",
}
BIN_DIR: Final = Path(__file__).parent / "bin"
_DROPPED: Final[Mapping[str, str]] = {"memory": "memory", "swap": "memory", "cpu": "cpu"}


class CreateFailedError(Exception):
    """The container was made but can't be used; it is removed before this is raised."""


@dataclass(frozen=True, slots=True)
class Settings:
    """What `docker()` was given, already validated (sandbox.py)."""

    image: str
    allow_internet: bool
    cpus: float | None
    memory_mb: int | None

    @property
    def nano_cpus(self) -> int | None:
        return None if self.cpus is None else round(self.cpus * 1_000_000_000)

    @property
    def memory_bytes(self) -> int | None:
        return None if self.memory_mb is None else self.memory_mb * 1024 * 1024


def container_name(operation_key: str) -> str:
    """The container's name, which is also its `SandboxId`: unique per key, so a repeated
    create answers 409 and is the same container."""
    return f"threads-{sha256_hex(operation_key.encode('utf-8'))[:32]}"


def volumes_of(container: str) -> tuple[str, str, str]:
    """The container's three named volumes, by its name: the exec volume (/run/threads), the
    workspace, and /tmp.

    /tmp is a volume, not a tmpfs: with ReadonlyRootfs the daemon refuses an archive PUT to any
    path that isn't inside a volume ("container rootfs is marked read-only"), and the kit puts
    a command's stdin and a tree import's archive under /tmp/threads (posix.RUN_DIR)."""
    suffix = container.removeprefix("threads-")
    return f"threads-exec-{suffix}", f"threads-ws-{suffix}", f"threads-tmp-{suffix}"


def request_body(name: str, operation_key: str, settings: Settings) -> Mapping[str, JsonValue]:
    exec_volume, workspace, tmp = volumes_of(name)
    host: dict[str, JsonValue] = {
        "Init": True,
        "ReadonlyRootfs": True,
        "PidsLimit": PIDS_LIMIT,
        "CapDrop": ["ALL"],
        # CHOWN is the supervisor's: `--idle` gives /workspace to uid 1000 before anything can
        # be admitted. Commands run as uid 1000, where the permitted set is empty.
        "CapAdd": ["SETUID", "SETGID", "KILL", "CHOWN"],
        "SecurityOpt": ["no-new-privileges"],
        "Mounts": [
            {"Type": "volume", "Source": exec_volume, "Target": ROOT},
            {
                "Type": "volume",
                "Source": workspace,
                "Target": WORKSPACE,
                "VolumeOptions": {"NoCopy": True},
            },
            {"Type": "volume", "Source": tmp, "Target": TMP, "VolumeOptions": {"NoCopy": True}},
        ],
    }
    if not settings.allow_internet:
        host["NetworkMode"] = "none"
    if (nano := settings.nano_cpus) is not None:
        host["NanoCpus"] = nano
    if (memory := settings.memory_bytes) is not None:
        host["Memory"], host["MemorySwap"] = memory, memory
    return {
        "Image": settings.image,
        "Entrypoint": [SUPERVISE, "--idle"],
        "Cmd": [],
        "User": "0",
        "Env": [PATH_ENV],
        "Healthcheck": {"Test": ["NONE"]},
        "Labels": {"threads.operation_key": operation_key},
        "WorkingDir": WORKSPACE,
        "HostConfig": host,
    }


async def create(engine: Engine, operation_key: str, settings: Settings) -> str:
    """The container's name, running with the supervisor idle and /workspace owned by uid
    1000. Raises what `driver.classify` names."""
    name = container_name(operation_key)
    warnings = await _created(engine, name, operation_key, settings)
    try:
        await _enforced(engine, name, warnings, settings)
        await _inject(engine, name, settings.image)
        await engine.start_container(name)
        await _usable(engine, name, settings.image)
    except (CreateFailedError, ConfigError):
        # A container that can't be used is never left behind for gc to find.
        await _remove(engine, name)
        raise
    return name


async def _created(
    engine: Engine, name: str, operation_key: str, settings: Settings
) -> Sequence[str]:
    """A 409 is this key's earlier create: that container is the one. A missing image is pulled
    anonymously, then the create is tried once more."""
    body = request_body(name, operation_key, settings)
    try:
        return await _make(engine, name, body)
    except EngineError as refused:
        if refused.status != NOT_FOUND:
            raise
        await engine.pull_image(settings.image)
        return await _make(engine, name, body)


async def _make(engine: Engine, name: str, body: Mapping[str, JsonValue]) -> Sequence[str]:
    try:
        return (await engine.create_container(name, body)).warnings
    except NameTakenError:
        return ()


async def _enforced(engine: Engine, name: str, warnings: Sequence[str], settings: Settings) -> None:
    """A limit the daemon dropped, named in the create's warnings or missing from the applied
    HostConfig, fails the create: a sandbox never runs with a limit its caller asked for
    silently gone."""
    asked = (settings.nano_cpus, settings.memory_bytes) != (None, None)
    dropped = next((limit for w in warnings if (limit := _dropped(w)) is not None), None)
    if dropped is None and asked:
        applied = await engine.inspect_container(name)
        if applied is None:
            raise EngineError(NOT_FOUND, f"{name} is gone right after its create")
        if settings.nano_cpus is not None and applied.host_config.nano_cpus == 0:
            dropped = "cpu"
        elif settings.memory_bytes is not None and applied.host_config.memory == 0:
            dropped = "memory"
    if dropped is not None:
        raise CreateFailedError(
            f"Docker can't enforce the {dropped} limit here "
            "(rootless Docker needs cgroup v2 delegation)"
        )


def _dropped(warning: str) -> str | None:
    lowered = warning.lower()
    return next((limit for word, limit in _DROPPED.items() if word in lowered), None)


async def _inject(engine: Engine, name: str, image: str) -> None:
    """The supervisor and an empty `state/`, both 0700 and owned by uid 0, into the exec
    volume. uid 1000 can't read, replace or run anything there."""
    described = await engine.inspect_image(image)
    if described is None:
        raise CreateFailedError(f"image {image} isn't available locally; run `docker pull {image}`")
    build = ARCHES.get(described.architecture)
    if build is None:
        raise ConfigError(
            "invalid_config",
            f"docker: image {image} is {described.architecture}, not amd64 or arm64",
        )
    tar = archive.build_tar(
        (
            archive.Entry("bin", 0o700, 0, 0),
            archive.Entry("bin/supervise", 0o700, 0, 0, _supervisor(build)),
            archive.Entry("state", 0o700, 0, 0),
        )
    )
    await engine.put_archive(name, ROOT, tar)


def _supervisor(build: str) -> bytes:
    """The shipped binary, refused unless it hashes to its pin."""
    binary = BIN_DIR / f"supervise-{build}"
    if not binary.is_file():
        missing = f"the {build} supervisor is missing: run scripts/build-supervisor.sh"
        raise CreateFailedError(missing)
    body = binary.read_bytes()
    if sha256_hex(body) != BINARIES[build]:
        raise CreateFailedError("the injected supervisor does not match its pinned sha256")
    return body


async def _usable(engine: Engine, name: str, image: str) -> None:
    """`supervise --check` as uid 0: it takes no lock and prints what the image carries. Its
    stdout is container output, so it is parsed strictly."""
    code, out, _ = await docker_exec.run_to_end(engine, name, (SUPERVISE, "--check"), "0")
    try:
        tools = Tools.model_validate_json(out)
    except ValidationError as broken:
        raise CreateFailedError(f"`supervise --check` answered {out!r}: {broken}") from broken
    if code != 0 or not (tools.sh and tools.env):
        raise CreateFailedError(f"image {image} lacks /bin/sh (docker() needs sh and env)")


async def _remove(engine: Engine, name: str) -> None:
    """Best effort: what it can't remove, gc does (the ledger's row outlives this call)."""
    try:
        await engine.remove_container(name)
        for volume in volumes_of(name):
            await engine.remove_volume(volume)
    except EngineError:
        return
