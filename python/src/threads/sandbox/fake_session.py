"""One fake sandbox: an in-memory file tree, and exec scripted by a conformance SandboxScript's
`tools` (the command's first word names the entry)."""

import asyncio
import posixpath
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Literal, NotRequired, TypedDict

from pydantic import ConfigDict, JsonValue, with_config

from threads.log import SnapshotData
from threads.log.digest import sha256_hex
from threads.log.jcs import canonicalize
from threads.loop.tools import Termination
from threads.result import Err, Ok
from threads.sandbox.protocol import (
    NO_ENV,
    ExecOutput,
    SandboxContext,
    SandboxError,
    SandboxId,
)

CHUNK = 64 * 1024
"""Exec output is streamed in chunks of this size, never as one buffer."""


@with_config(ConfigDict(extra="forbid", strict=True))
class ToolScript(TypedDict):
    output: str
    is_error: NotRequired[bool]
    executed_keys: NotRequired[dict[str, str]]
    lookup: NotRequired[dict[str, JsonValue]]
    process: NotRequired[Literal["running", "terminated", "unknown"]]


@with_config(ConfigDict(extra="forbid", strict=True))
class ManifestEntry(TypedDict):
    path: str
    mode: int
    size: int
    sha256: str


async def fenced(context: SandboxContext) -> Err[SandboxError] | None:
    """The fake's provider dispatch point: a stale owner calls nothing."""
    passed = await context.fence()
    return (
        Err(SandboxError("stale_epoch", passed.error.message)) if isinstance(passed, Err) else None
    )


def manifest_of(files: Mapping[str, bytes]) -> list[ManifestEntry]:
    """The captured file tree: path, mode, size and sha256 per file, sorted by path."""
    return [
        ManifestEntry(path=path, mode=0o644, size=len(data), sha256=sha256_hex(data))
        for path, data in sorted(files.items())
    ]


def manifest_hash(manifest: list[ManifestEntry]) -> str:
    """The manifest's canonical hash."""
    value: list[JsonValue] = [
        {"path": e["path"], "mode": e["mode"], "size": e["size"], "sha256": e["sha256"]}
        for e in manifest
    ]
    match canonicalize(value):
        case Ok(value=text):
            return sha256_hex(text.encode("utf-8"))
        case Err(error=reason):
            raise ValueError(reason)


class FakeSession:
    def __init__(
        self,
        ident: SandboxId,
        files: Mapping[str, bytes],
        tools: Mapping[str, ToolScript],
        capture: Callable[["FakeSession", str], SnapshotData],
        close: Callable[["FakeSession"], Ok[None] | Err[SandboxError]],
    ) -> None:
        self._id = ident
        self.files: dict[str, bytes] = dict(files)
        self._tools = tools
        self._capture = capture
        self._close = close
        self._processes: dict[str, str] = {}
        """process_key -> the scripted tool it ran."""

    @property
    def id(self) -> SandboxId:
        return self._id

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
        if not command:
            raise ValueError("exec needs a command")
        bad = _invalid(cwd) or await fenced(context)
        if bad is not None:
            return bad
        tool = self._tools.get(command[0])
        if tool is None:
            missing = f"fake: command not found: {command[0]}\n".encode()
            return Ok(_output(127, b"", missing))
        self._processes[process_key] = command[0]
        # A key the provider already executed returns that output again (provider dedup).
        text = tool.get("executed_keys", {}).get(process_key, tool["output"])
        return Ok(_output(1 if tool.get("is_error") else 0, text.encode("utf-8"), b""))

    async def terminate(
        self, process_key: str, context: SandboxContext
    ) -> Ok[Termination] | Err[SandboxError]:
        stale = await fenced(context)
        if stale is not None:
            return stale
        name = self._processes.get(process_key)
        if name is None:
            return Ok("already_exited")
        match self._tools[name].get("process"):
            case "unknown":
                return Ok("unknown")
            case "terminated" | "running" | None:
                return Ok("terminated")

    async def upload(
        self, path: str, data: bytes, context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]:
        bad = _invalid(path) or self._directory(path) or await fenced(context)
        if bad is not None:
            return bad
        self.files[path] = data
        return Ok(None)

    async def download(self, path: str, context: SandboxContext) -> Ok[bytes] | Err[SandboxError]:
        bad = _invalid(path) or self._directory(path) or await fenced(context)
        if bad is not None:
            return bad
        data = self.files.get(path)
        return Err(SandboxError("not_found", f"no file {path}")) if data is None else Ok(data)

    async def snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SnapshotData] | Err[SandboxError]:
        stale = await fenced(context)
        return stale or Ok(self._capture(self, operation_key))

    async def close(self, context: SandboxContext) -> Ok[None] | Err[SandboxError]:
        stale = await fenced(context)
        return stale or self._close(self)

    def _directory(self, path: str) -> Err[SandboxError] | None:
        prefix = path.rstrip("/") + "/"
        if any(name.startswith(prefix) for name in self.files):
            return Err(SandboxError("is_directory", f"{path} is a directory"))
        return None


def _invalid(path: str) -> Err[SandboxError] | None:
    # Absolute and already normal: no relative parts, no `..` escape.
    if not path.startswith("/") or posixpath.normpath(path) != path:
        return Err(SandboxError("invalid_path", f"not an absolute normal path: {path}"))
    return None


def _output(code: int, stdout: bytes, stderr: bytes) -> ExecOutput:
    exit_code: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    exit_code.set_result(code)
    return ExecOutput(exit_code, _chunks(stdout), _chunks(stderr))


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(data), CHUNK):
        yield data[start : start + CHUNK]
