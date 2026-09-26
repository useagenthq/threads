"""The OS confinement `dev_sandbox()` runs every command inside (lane 16 D3). It is required: a
platform without one is refused at setup, never run unconfined. Nothing here proves a process
ended; termination stays unconfirmed.

Linux is bubblewrap: private pid, ipc, uts and cgroup namespaces, no network unless the caller
asked for it, a private /proc, /dev, /tmp and /run, read-only system paths, the sandbox
directory as /workspace and no home, an exact environment, capabilities dropped, and the wrapper
dying with its parent.

macOS is sandbox-exec with a deny-by-default profile: the same reachable paths, network denied,
other processes' info denied, and a fixed list of Mach lookups. macOS has no mount namespace, so
the directory keeps its host path there and `guest()` rewrites /workspace to it (Linux returns
the path unchanged), /tmp is denied outright instead of being made private, and the profile
carries the call's environment as the spawn's.
"""

import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from threads.result import Err, Ok

WORKSPACE = "/workspace"

DEFAULT_TOOL: Mapping[str, str] = {"linux": "bwrap", "darwin": "sandbox-exec"}
"""The confinement program each platform uses when the caller names none."""

_LINUX_SYSTEM = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32")
"""Read-only system paths; `-try` skips one the host doesn't have."""

_LINUX_ETC = (
    "/etc/alternatives",
    "/etc/ca-certificates",
    "/etc/group",
    "/etc/localtime",
    "/etc/nsswitch.conf",
    "/etc/passwd",
    "/etc/resolv.conf",
    "/etc/ssl",
)
"""A minimal /etc: name resolution, certificates and the time zone, never the host's config."""

_MACOS_READ = (
    "/usr",
    "/bin",
    "/sbin",
    "/System",
    "/Library",
    "/dev",
    "/private/etc",
    "/private/var/db",
    "/private/var/select",
    "/opt/homebrew",
    "/opt/local",
)

_MACOS_MACH = (
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.system.opendirectoryd.membership",
    "com.apple.system.notification_center",
    "com.apple.system.logger",
)
"""The Mach services a command needs to start and to look a user up; nothing else is reachable."""

_MACOS_WRITE_DEV = ("/dev/null", "/dev/zero", "/dev/tty", "/dev/dtracehelper")
"""The character devices a command writes to; the rest of /dev stays read-only."""

_GUEST_ROOT = re.compile(r"/workspace(?=$|[/\s:'\"])")
"""/workspace as a whole path component, which macOS rewrites to the host directory."""


@dataclass(frozen=True, slots=True)
class ConfinedSpec:
    directory: str
    """The sandbox's host directory, which becomes its /workspace."""
    command: Sequence[str]
    cwd: str
    """The working directory as the command sees it (a `guest()` path)."""
    env: Mapping[str, str]
    """Exactly this environment; nothing of the host's is inherited."""


def _seatbelt(value: str) -> str:
    """A Seatbelt string literal: only `"` and `\\` are special."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _profile(directory: str, allow_internet: bool) -> str:
    read = " ".join(f"(subpath {_seatbelt(at)})" for at in _MACOS_READ)
    write_dev = " ".join(f"(literal {_seatbelt(at)})" for at in _MACOS_WRITE_DEV)
    mach = " ".join(f"(global-name {_seatbelt(name)})" for name in _MACOS_MACH)
    return "\n".join(
        (
            "(version 1)",
            "(deny default)",
            "(allow process-fork)",
            "(allow process-exec*)",
            "(allow sysctl-read)",
            "(allow ipc-posix-shm*)",
            "(allow signal (target self))",
            "(allow process-info-pidinfo (target self))",
            # stat() of a path's ancestors, which path resolution needs. It reads no content
            # and lists no directory: a denied file's bytes stay denied.
            "(allow file-read-metadata)",
            f'(allow file-read* (literal "/") {read})',
            f"(allow file-read* file-write* (subpath {_seatbelt(directory)}))",
            f"(allow file-write-data {write_dev})",
            f"(allow mach-lookup {mach})",
            "(allow network*)" if allow_internet else "(deny network*)",
        )
    )


@dataclass(frozen=True, slots=True)
class Confinement:
    tool: str
    """The program that confines, named in a capability_missing message."""
    platform: str
    allow_internet: bool

    def guest(self, directory: str, path: str) -> str:
        """The path a confined command sees for the sandbox path `path`."""
        if self.platform != "darwin":
            return path
        # No mount namespace: the directory keeps its host path there, so every /workspace a
        # command is given names it instead. A command's own output still shows the host path.
        return _GUEST_ROOT.sub(directory, path)

    def spawn_env(self, env: Mapping[str, str]) -> dict[str, str]:
        """The environment the wrapper process itself is spawned with. bwrap carries the call's
        env through --setenv after --clearenv; sandbox-exec has no such flag, so the call's env
        is the spawn's. Either way the command sees exactly the call's names."""
        return {} if self.platform == "linux" else dict(env)

    def wrap(self, spec: ConfinedSpec) -> list[str]:
        """The confined argv to spawn."""
        if self.platform == "linux":
            return [self.tool, *self._bwrap(spec)]
        command = [self.guest(spec.directory, word) for word in spec.command]
        return [self.tool, "-p", _profile(spec.directory, self.allow_internet), "--", *command]

    def _bwrap(self, spec: ConfinedSpec) -> list[str]:
        binds: list[str] = []
        for at in (*_LINUX_SYSTEM, *_LINUX_ETC):
            binds += ["--ro-bind-try", at, at]
        env: list[str] = []
        for name, value in spec.env.items():
            env += ["--setenv", name, value]
        return [
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup-try",
            *([] if self.allow_internet else ["--unshare-net"]),
            "--proc",
            "/proc",
            # /dev is not in the sub-lane's list, but without /dev/null nothing runs.
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",  # noqa: S108 - a path inside the confinement, not on the host
            "--tmpfs",
            "/run",
            *binds,
            "--bind",
            spec.directory,
            WORKSPACE,
            "--chdir",
            spec.cwd,
            "--clearenv",
            *env,
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--",
            *spec.command,
        ]


_UNSUPPORTED = (
    "dev_sandbox() confines with bubblewrap on Linux and sandbox-exec on macOS; {} has neither"
)


def confinement(allow_internet: bool, tool: str | None) -> Ok[Confinement] | Err[str]:
    """The platform's confinement, or what is missing (setup answers capability_missing)."""
    platform = sys.platform
    named = tool if tool else DEFAULT_TOOL.get(platform)
    if named is None or platform not in DEFAULT_TOOL:
        return Err(_UNSUPPORTED.format(platform))
    return Ok(Confinement(named, platform, allow_internet))


def probe(confined: Confinement, directory: str) -> str | None:
    """Runs the confinement over `true` in `directory`: what setup proves, so a missing bwrap, a
    kernel with user namespaces disabled, or a refused profile is named before any agent runs."""
    argv = confined.wrap(
        ConfinedSpec(directory, ["/usr/bin/true"], confined.guest(directory, WORKSPACE), {})
    )
    try:
        ran = subprocess.run(  # noqa: S603 - argv, no shell; the program is the named tool
            argv, cwd=directory, env={}, capture_output=True, check=False, timeout=30
        )
    except OSError as error:
        return f"{confined.tool} can't run ({error})"
    except subprocess.SubprocessError as error:
        return f"{confined.tool} can't run ({error})"
    if ran.returncode == 0:
        return None
    why = ran.stderr.decode("utf-8", "replace").strip().split("\n")[-1]
    return f"{confined.tool} refused to confine a command ({why or f'exit {ran.returncode}'})"
