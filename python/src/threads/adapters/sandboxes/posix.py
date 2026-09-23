"""What every sandbox adapter runs inside its sandbox, over the provider's own exec.

- exec starts the command through a wrapper that runs it under `env -i` with exactly the names
  given, and feeds stdin from a file uploaded first. The values reach the sandbox through the
  provider API (never the provider's argv), under carrier names; inside the sandbox the wrapper
  hands them to `env -i` as `NAME=value` arguments.
- the manifest is computed in the sandbox over /workspace, paths relative.

Nothing here proves a process ended or a sandbox was quiescent: anything the guest can write,
it can forge. Termination and quiescence come only from provider primitives.
The image needs the tools spec/schema/README.md lists ("Sandbox image").
"""

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from threads.log.digest import sha256_hex
from threads.result import Err, Ok
from threads.sandbox.admit import admit_exec
from threads.sandbox.manifest import ManifestEntry, in_order
from threads.sandbox.protocol import (
    NO_ENV,
    ExecOutput,
    SandboxContext,
    SandboxError,
    invalid_path,
)

RUN_DIR = "/tmp/threads"  # noqa: S108 - a path inside the sandbox, not on the host
WORKSPACE = "/workspace"

WRAPPER = r"""__t_keep=$1; __t_in=$2; shift 2
case "$1" in
  */*) __t_c=$1 ;;
  *) __t_c=; IFS=:; for __t_p in $PATH; do
       if [ -f "$__t_p/$1" ] && [ -x "$__t_p/$1" ]; then __t_c=$__t_p/$1; break; fi
     done; unset IFS ;;
esac
[ -n "$__t_c" ] || { echo "threads: command not found: $1" >&2; exit 127; }
shift
set -- "$__t_c" "$@"
for __t_k in $__t_keep; do
  eval "__t_v=\${__t_v_$__t_k}"
  set -- "$__t_k=$__t_v" "$@"
done
exec env -i "$@" < "${__t_in:-/dev/null}"
"""
"""$1 the env names to keep (shell names, checked by `admit_exec`), $2 the stdin file (empty:
none), then the command. Each kept value arrives as `__t_v_<name>` (see `wrap`), so the wrapper
shell never takes a tool value as its own PATH, PWD, IFS or SHLVL. The command is resolved on
the provider's PATH, then runs under `env -i` with only the kept names: nothing the provider
set, whatever its name, is inherited."""

MANIFEST = r"""cd /workspace 2>/dev/null || exit 0
find . -type f -exec sh -c 'for p do
  printf "%s\0%s\0%s\0" "${p#./}" "$(stat -c %a "$p")" "$(stat -c %s "$p")"
  sha256sum < "$p" | cut -c1-64 | tr -d "\n"; printf "\0"
done' sh {} +
"""
"""NUL-separated path, octal mode, size, sha256 for every regular file under /workspace."""


class Primitives(Protocol):
    """What a provider's session supplies: its own exec and file write, each fenced at its
    transport."""

    async def start(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        context: SandboxContext,
        process_key: str | None = None,
    ) -> Ok[ExecOutput] | Err[SandboxError]:
        """Starts `argv` as the provider's exec, `env` passed through the provider API.
        `process_key` names the provider's own record of the process, when it keeps one."""
        ...

    async def upload(
        self, path: str, data: bytes, context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]: ...


def stdin_path(process_key: str) -> str:
    """Where a process key's stdin is uploaded: a file-safe name."""
    return f"{RUN_DIR}/{sha256_hex(process_key.encode('utf-8'))[:32]}.stdin"


@dataclass(frozen=True, slots=True)
class Wrapped:
    """One exec as the provider runs it: `argv` for its exec, `env` for its env parameter."""

    argv: tuple[str, ...]
    env: Mapping[str, str]


def wrap(command: Sequence[str], env: Mapping[str, str], stdin: str | None = None) -> Wrapped:
    """The provider exec that runs `command` with exactly `env`, stdin from the file `stdin`.
    The call is admitted here (copied and checked), so a caller bug raises before any send."""
    command, env = admit_exec(command, env)
    names = " ".join(sorted(env))
    return Wrapped(
        argv=("/bin/sh", "-c", WRAPPER, "threads", names, stdin or "", *command),
        env={f"__t_v_{name}": value for name, value in env.items()},
    )


async def run(  # noqa: PLR0913 - the options spec/api.json names for exec
    session: Primitives,
    command: Sequence[str],
    context: SandboxContext,
    *,
    process_key: str,
    cwd: str,
    env: Mapping[str, str],
    stdin: bytes | None,
) -> Ok[ExecOutput] | Err[SandboxError]:
    """spec/api.json `SandboxSession.exec` over the provider's exec."""
    fed = None if stdin is None else stdin_path(process_key)
    wrapped = wrap(command, env, fed)
    bad = invalid_path(cwd)
    if bad is not None:
        return bad
    if fed is not None and stdin is not None:
        uploaded = await session.upload(fed, stdin, context)
        if isinstance(uploaded, Err):
            return uploaded
    return await session.start(wrapped.argv, wrapped.env, cwd, context, process_key)


async def manifest(
    session: Primitives, context: SandboxContext
) -> Ok[list[ManifestEntry]] | Err[SandboxError]:
    """The manifest of /workspace as it is in the sandbox now."""
    started = await session.start(("/bin/sh", "-c", MANIFEST), NO_ENV, "/", context)
    if isinstance(started, Err):
        return started
    code, out, err = await collect(started.value)
    if code != 0:
        message = err.decode("utf-8", "replace")
        return Err(SandboxError("unavailable", f"the manifest failed: {message}"))
    return parse_manifest(out)


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    path: Annotated[str, StringConstraints(min_length=1)]
    mode: Annotated[str, StringConstraints(pattern=r"^[0-7]{1,4}$")]
    size: Annotated[str, StringConstraints(pattern=r"^[0-9]+$")]
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def parse_manifest(out: bytes) -> Ok[list[ManifestEntry]] | Err[SandboxError]:
    """The manifest script's output: groups of four NUL-terminated fields."""
    fields = out.split(b"\0")
    if fields[-1] != b"" or (len(fields) - 1) % 4:
        return Err(SandboxError("unavailable", "the manifest output is malformed"))
    entries: list[ManifestEntry] = []
    for at in range(0, len(fields) - 1, 4):
        try:
            path, mode, size, digest = (f.decode("utf-8") for f in fields[at : at + 4])
            record = _Record(path=path, mode=mode, size=size, sha256=digest)
        except (UnicodeDecodeError, ValidationError) as error:
            return Err(SandboxError("unavailable", f"the manifest output is malformed: {error}"))
        entries.append(
            ManifestEntry(
                path=record.path,
                mode=int(record.mode, 8),
                size=int(record.size),
                sha256=record.sha256,
            )
        )
    return Ok(in_order(entries))


async def collect(output: ExecOutput) -> tuple[int, bytes, bytes]:
    """A command's whole output; only for small outputs (the manifest, tests)."""
    out, err = await asyncio.gather(_read(output.stdout), _read(output.stderr))
    return await output.exit_code, out, err


async def _read(stream: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in stream])
