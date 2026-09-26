"""`dev_sandbox()`: a local sandbox in a directory on the host, inside the platform's OS
confinement (confine.py). What it declares, and why:

- Confinement is required. Without one, setup fails capability_missing naming what is missing; a
  command never runs unconfined.
- Create: the directory is named by the operation key, so a lost create answer is settled by
  looking for that exact directory. Nothing else can have made it, so lookup is final.
- Termination: killing the process group proves nothing about a descendant that left it, so
  termination is unconfirmed and a crashed exec parks.
- Snapshots: none. capture_classes is empty, snapshot is unavailable, and restore is
  snapshot_missing.
- Egress: denied by the confinement (no network namespace on Linux, `(deny network*)` on macOS);
  `allow_internet` lifts it and the adapter declares egress unenforced.
- Credentials: a command gets exactly the env of its call and nothing of the host's (invariant 4),
  and the confinement gives it no home and no /run.
"""

import os
import tempfile
from dataclasses import dataclass
from typing import Literal

from threads.adapters.sandboxes.dev.confine import Confinement, confinement, probe
from threads.adapters.sandboxes.dev.session import DevSession
from threads.agents.config import ConfigError
from threads.log.digest import sha256_hex
from threads.loop.model import Found, LookupResult, NotFound
from threads.result import Err, Ok
from threads.sandbox.protocol import (
    Looked,
    LookupSupport,
    SandboxContext,
    SandboxError,
    SandboxInfo,
    SandboxSession,
    refused,
)

_PREFIX = "threads-dev-"


def _name_of(operation_key: str) -> str:
    """The directory one operation key owns: the same key always names the same one."""
    return f"{_PREFIX}{sha256_hex(operation_key.encode())[:32]}"


@dataclass(frozen=True, slots=True)
class _Ready:
    """The dev root as it really is, with the confinement proven over it."""

    confined: Confinement
    root: str


class DevSandbox:
    """spec/api.json `Sandbox` in a local confined directory (module docstring)."""

    def __init__(
        self,
        *,
        root: str | None = None,
        allow_internet: bool = False,
        tool: str | None = None,
    ) -> None:
        self._given = root or os.path.join(tempfile.gettempdir(), "threads-dev")
        self._tool = tool
        self._ready: _Ready | None = None
        self._info = SandboxInfo(
            provider="dev",
            egress="unenforced" if allow_internet else "enforced",
            capture_classes=(),
            browser="none",
            desktop="none",
            lookup=LookupSupport(create="final", snapshot="none"),
            termination="unconfirmed",
        )
        self._allow_internet = allow_internet

    @property
    def info(self) -> SandboxInfo:
        return self._info

    async def setup(self) -> None:
        """Proves the confinement on the host, before any agent runs."""
        self._prove()

    def _prove(self) -> _Ready:
        """The confinement and the dev root. The root is resolved through its own symlinks once,
        because macOS's profile and the host paths must name the same directory (its temp dir is
        reached through a symlink)."""
        if self._ready is not None:
            return self._ready
        confined = confinement(self._allow_internet, self._tool)
        if isinstance(confined, Err):
            raise ConfigError("capability_missing", confined.error)
        os.makedirs(self._given, exist_ok=True)
        root = os.path.realpath(self._given)
        missing = probe(confined.value, root)
        if missing is not None:
            raise ConfigError(
                "capability_missing", f"dev_sandbox() needs an OS confinement: {missing}"
            )
        self._ready = _Ready(confined.value, root)
        return self._ready

    def _session(self, name: str) -> DevSession:
        ready = self._prove()
        return DevSession(os.path.join(ready.root, name), name, ready.confined)

    async def create(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        ready = self._prove()
        name = _name_of(operation_key)
        at = os.path.join(ready.root, name)
        refusal = await refused(context)
        if refusal is not None:
            return refusal
        # One key, one directory: a second create of the same key finds its own.
        try:
            os.mkdir(at, 0o700)
        except FileExistsError:
            pass
        except OSError as error:
            return Err(SandboxError("unavailable", f"{at}: {error}"))
        return Ok(self._session(name))

    async def restore(
        self, snapshot_id: str, manifest_hash: str, operation_key: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        refusal = await refused(context)
        if refusal is not None:
            return refusal
        return Err(SandboxError("snapshot_missing", "the dev sandbox has no snapshots"))

    async def lookup(self, operation_key: str, context: SandboxContext) -> Looked[SandboxSession]:
        ready = self._prove()
        name = _name_of(operation_key)
        refusal = await refused(context)
        if refusal is not None:
            return refusal
        # Final: only this key's create can have made this directory.
        answer: LookupResult[SandboxSession] = (
            Found(self._session(name))
            if os.path.isdir(os.path.join(ready.root, name))
            else NotFound()
        )
        return Ok(answer)

    async def attach(
        self, ref: str, context: SandboxContext
    ) -> Ok[SandboxSession] | Err[SandboxError]:
        ready = self._prove()
        if not ref.startswith(_PREFIX) or "/" in ref:
            return Err(SandboxError("resource_unknown", f"not a dev sandbox: {ref}"))
        refusal = await refused(context)
        if refusal is not None:
            return refusal
        if not os.path.isdir(os.path.join(ready.root, ref)):
            return Err(SandboxError("not_found", f"no live sandbox {ref}"))
        return Ok(self._session(ref))

    async def release(
        self, ref: str, context: SandboxContext
    ) -> Ok[Literal["released", "already_gone"]] | Err[SandboxError]:
        refusal = await refused(context)
        if refusal is not None:
            return refusal
        return Err(SandboxError("unavailable", f"the dev sandbox has no snapshots ({ref})"))


def dev_sandbox(
    *, root: str | None = None, allow_internet: bool = False, tool: str | None = None
) -> DevSandbox:
    """A local confined sandbox provider (the `dev_sandbox()` of spec/api.json
    conventions.adapters). `root` is where sandbox directories are made, `tool` the confinement
    program."""
    return DevSandbox(root=root, allow_internet=allow_internet, tool=tool)
