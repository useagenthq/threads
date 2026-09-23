"""A provider-agnostic sandbox service for adapter tests: what E2B, Daytona or Modal would hold,
in memory. Each adapter's test shim speaks its provider's wire protocol and calls into this.
The shell side emulates the scripts threads runs in a sandbox (adapters/sandboxes/posix.py)
and a conformance SandboxScript's tools; snapshots named in the script restore to their
`restore_sandbox_id` with the scripted manifest, and may lose the answer or crash the host."""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import JsonValue, TypeAdapter

from threads.adapters.sandboxes import posix
from threads.sandbox.fake import FakeCrashError, SandboxScript, SnapshotScript
from threads.sandbox.manifest import ManifestEntry, manifest_of

_SCRIPT: TypeAdapter[SandboxScript] = TypeAdapter(SandboxScript)
KILLED = 137


class LostAnswerError(Exception):
    """The provider acted, then its answer was lost: the shim fails the transport."""


class UnavailableError(Exception):
    """The provider refused this call (a 5xx): nothing was done."""


@dataclass
class Proc:
    """One process group: its whole output, and its exit once it ends."""

    stdout: bytes
    stderr: bytes
    exit: "asyncio.Future[int]"

    @property
    def running(self) -> bool:
        return not self.exit.done()


@dataclass
class Box:
    id: str
    key: str | None
    """The operation key it was created under (a tag, label, metadata value or name)."""
    files: dict[str, bytes]
    manifest: list[ManifestEntry] | None = None
    """A scripted tree's manifest, restored from a conformance snapshot."""
    processes: dict[str, Proc] = field(default_factory=dict[str, Proc])
    """By the provider's record of each (a tag, an exec id)."""
    orphans: list[Proc] = field(default_factory=list[Proc])
    alive: bool = True


@dataclass
class Snap:
    id: str
    key: str | None
    files: dict[str, bytes]
    manifest: list[ManifestEntry] | None
    script: SnapshotScript | None = None
    frozen: bool = False


@dataclass
class FakeBackend:
    """Counters are what tests assert: provider calls that reached the service."""

    script: SandboxScript = field(default_factory=SandboxScript)
    boxes: dict[str, Box] = field(default_factory=dict[str, Box])
    snaps: dict[str, Snap] = field(default_factory=dict[str, Snap])
    requests: int = 0
    """Every request that reached the service."""
    creates: int = 0
    """Create and restore calls."""
    releases: int = 0
    """Sandbox kills and snapshot deletes."""
    fail_releases: int = 0
    lose_creates: int = 0
    """Lose the answer of this many plain creates, after creating."""
    crash_restores: int = 0
    """The host dies right after this many restores created their sandbox."""
    envs: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    """Every environment handed to a sandbox: at create and at every exec."""
    argvs: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])
    around_capture: tuple[Callable[[Box], None], Callable[[Box], None]] | None = None
    """Guest writes just before and just after the next capture."""
    _next: int = 0

    def __post_init__(self) -> None:
        for name, snap in self.script.get("snapshots", {}).items():
            self.snaps[name] = Snap(name, None, {}, snap["manifest"], snap)

    @classmethod
    def scripted(cls, script: Mapping[str, JsonValue] | None = None) -> "FakeBackend":
        return cls(_SCRIPT.validate_python(script or {}))

    def hit(self) -> None:
        self.requests += 1

    # The control plane.

    def create(self, key: str | None, env: Mapping[str, str]) -> Box:
        self.creates += 1
        self.envs.append(dict(env))
        box = Box(self._name("sbx"), key, {})
        self.boxes[box.id] = box
        if self.lose_creates:
            self.lose_creates -= 1
            raise LostAnswerError(box.id)
        return box

    def restore(self, snapshot_id: str, key: str | None, env: Mapping[str, str]) -> Box | None:
        """None: no such snapshot."""
        snap = self.snaps.get(snapshot_id)
        if snap is None:
            return None
        self.creates += 1
        self.envs.append(dict(env))
        ident = snap.script["restore_sandbox_id"] if snap.script else self._name("sbx")
        box = Box(ident, key, dict(snap.files), snap.manifest)
        self.boxes[box.id] = box
        if self.crash_restores:
            self.crash_restores -= 1
            raise FakeCrashError(f"the host died after restoring {snapshot_id}")
        match snap.script.get("restore_response") if snap.script else None:
            case "lost":
                raise LostAnswerError(box.id)
            case "crash":
                raise FakeCrashError(f"the host died after restoring {snapshot_id}")
            case "ok" | None:
                return box

    def find(self, key: str) -> Box | None:
        """A live sandbox by its operation key. A script that makes lookup unsupported makes
        the provider fail the query."""
        scripted = self.script.get("snapshots", {}).values()
        if any(s.get("create_lookup") == "unsupported" for s in scripted):
            raise UnavailableError("lookup by key is unavailable")
        return next((b for b in self.boxes.values() if b.alive and b.key == key), None)

    def get(self, box_id: str) -> Box | None:
        box = self.boxes.get(box_id)
        return box if box is not None and box.alive else None

    def kill(self, box_id: str) -> bool:
        """False: it was already gone."""
        self.releases += 1
        self._maybe_fail()
        box = self.get(box_id)
        if box is None:
            return False
        box.alive = False
        for proc in (*box.processes.values(), *box.orphans):
            _end(proc, KILLED)
        return True

    def snapshot(self, box: Box, key: str | None, *, frozen: bool) -> Snap:
        """`frozen`: the provider paused the whole sandbox for the capture. The image, and
        its manifest, are the files at the capture instant."""
        before, after = self.around_capture or (_nothing, _nothing)
        self.around_capture = None
        before(box)
        manifest = box.manifest if box.manifest is not None else _workspace(box.files)
        snap = Snap(self._name("snap"), key, dict(box.files), manifest, frozen=frozen)
        self.snaps[snap.id] = snap
        after(box)
        return snap

    def stop(self, box: Box) -> None:
        """A whole-sandbox stop: every process ends."""
        for proc in (*box.processes.values(), *box.orphans):
            _end(proc, KILLED)

    def find_snapshot(self, key: str) -> Snap | None:
        return next((s for s in self.snaps.values() if s.key == key), None)

    def delete_snapshot(self, snapshot_id: str) -> bool:
        self.releases += 1
        self._maybe_fail()
        return self.snaps.pop(snapshot_id, None) is not None

    # The data plane.

    def write(self, box: Box, path: str, data: bytes) -> None:
        box.files[path] = data

    def read(self, box: Box, path: str) -> bytes | None:
        return box.files.get(path)

    def is_directory(self, box: Box, path: str) -> bool:
        prefix = path.rstrip("/") + "/"
        return any(name.startswith(prefix) for name in box.files)

    def run(
        self, box: Box, argv: Sequence[str], env: Mapping[str, str], tag: str | None = None
    ) -> Proc:
        """Runs `argv` as the provider's exec would, with `env` from the provider API. `tag`
        is the provider's own record of the process, when the provider keeps one."""
        self.envs.append(dict(env))
        self.argvs.append(tuple(argv))
        match tuple(argv):
            case ("/bin/sh", "-c", posix.WRAPPER, _, keep, stdin, *command):
                # As the wrapper does: each kept value arrives under its `__t_v_` carrier.
                seen = {k: env[f"__t_v_{k}"] for k in keep.split() if f"__t_v_{k}" in env}
                fed = box.files.get(stdin, b"") if stdin else b""
                proc = self._command(box, command, seen, fed)
            case ("/bin/sh", "-c", posix.MANIFEST):
                manifest = box.manifest if box.manifest is not None else _workspace(box.files)
                proc = _done(0, _nul(manifest))
            case _:
                proc = _done(127, b"", f"sh: {argv[0]}: not found\n".encode())
        box.processes[tag or self._name("proc")] = proc
        return proc

    def signal(self, box: Box, tag: str) -> bool:
        """The provider's kill of the process it recorded under `tag`: False when none runs.
        Descendants a command detached are not the provider's record and keep running."""
        proc = box.processes.get(tag)
        if proc is None or not proc.running:
            return False
        _end(proc, KILLED)
        return True

    def _command(
        self, box: Box, command: Sequence[str], env: Mapping[str, str], stdin: bytes
    ) -> Proc:
        """A SandboxScript tool by the command's first word, else a few builtins."""
        name, args = command[0], command[1:]
        tool = self.script.get("tools", {}).get(name)
        if tool is not None:
            return _done(1 if tool.get("is_error") else 0, tool["output"].encode())
        if list(args) == ["-c", "cat >&2"]:
            return _done(0, b"", stdin)
        if name == "sh":
            # `sh -c 'setsid sleep 1000 &'`: a descendant no provider record names, still running.
            box.orphans.append(_running())
            return _done(0)
        return _builtin(name, args, env, stdin)

    def _maybe_fail(self) -> None:
        if self.fail_releases:
            self.fail_releases -= 1
            raise UnavailableError("the provider failed to release")

    def _name(self, prefix: str) -> str:
        self._next += 1
        return f"{prefix}_{self._next}"


def _builtin(name: str, args: Sequence[str], env: Mapping[str, str], stdin: bytes) -> Proc:
    match name:
        case "echo":
            return _done(0, (" ".join(args) + "\n").encode())
        case "printenv":
            return _done(0, "".join(f"{k}={v}\n" for k, v in sorted(env.items())).encode())
        case "cat" if not args:
            return _done(0, stdin)
        case "sleep":
            return _running()
        case _:
            return _done(127, b"", f"threads: command not found: {name}\n".encode())


def _nothing(_box: Box) -> None:
    return None


def _workspace(files: Mapping[str, bytes]) -> list[ManifestEntry]:
    """What the manifest script sees: regular files under /workspace only."""
    return manifest_of({p: d for p, d in files.items() if p.startswith(posix.WORKSPACE + "/")})


def _nul(manifest: list[ManifestEntry]) -> bytes:
    return b"".join(
        f"{e['path']}\0{e['mode']:o}\0{e['size']}\0{e['sha256']}\0".encode() for e in manifest
    )


def _done(code: int, stdout: bytes = b"", stderr: bytes = b"") -> Proc:
    exit_code: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    exit_code.set_result(code)
    return Proc(stdout, stderr, exit_code)


def _running() -> Proc:
    return Proc(b"", b"", asyncio.get_running_loop().create_future())


def _end(proc: Proc, code: int) -> None:
    if proc.running:
        proc.exit.set_result(code)
