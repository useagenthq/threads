"""The sandbox layer's exec: an adapter's `ExecOutput` streams
become an `ExecResult` of head and tail previews. Every chunk is spilled to the artifact store
as it arrives, so full output is never held in host memory; it becomes `full_output` exactly
when a stream outgrows its preview, and is durable before the result is returned."""

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field

from threads.log import ArtifactRef, Spill
from threads.result import Err, Ok
from threads.sandbox.protocol import (
    NO_ENV,
    ExecOutput,
    ExecResult,
    SandboxContext,
    SandboxError,
    SandboxSession,
)
from threads.store.spill import Spill as Sink

_MARKER = "\n[output truncated: {n} bytes in all; the full output is in full_output]\n"


@dataclass(frozen=True, slots=True)
class Command:
    """One exec: the command and the options spec/api.json names."""

    argv: Sequence[str]
    process_key: str
    """The call's effect key: the process group's durable identity."""
    cwd: str = "/workspace"
    env: Mapping[str, str] = field(default_factory=lambda: NO_ENV)
    timeout_ms: int | None = None
    stdin: bytes | None = None


@dataclass(slots=True)
class _Preview:
    """One stream's preview: all of it while it fits, else its head and tail."""

    limits: Spill
    whole: bytearray = field(default_factory=bytearray)
    tail: bytearray = field(default_factory=bytearray)
    head: bytes = b""
    total: int = 0

    @property
    def truncated(self) -> bool:
        return self.total > self.limits.threshold_bytes

    def add(self, chunk: bytes) -> None:
        fitted = not self.truncated
        self.total += len(chunk)
        if fitted:
            self.whole += chunk
            if not self.truncated:
                return
            # This chunk outgrew the preview: keep only the head and the tail from now on.
            self.head, self.tail = bytes(self.whole[: self.limits.head_bytes]), self.whole
            self.whole = bytearray()
        else:
            self.tail += chunk
        del self.tail[: max(0, len(self.tail) - self.limits.tail_bytes)]

    def text(self) -> str:
        if not self.truncated:
            return self.whole.decode("utf-8", "replace")
        head, tail = self.head.decode("utf-8", "ignore"), self.tail.decode("utf-8", "ignore")
        return head + _MARKER.format(n=self.total) + tail


_stopping: set[asyncio.Task[object]] = set()
"""Best-effort terminates still running after their exec answered timeout."""


async def run_exec(
    session: SandboxSession, command: Command, context: SandboxContext, spill: Sink, limits: Spill
) -> Ok[ExecResult] | Err[SandboxError]:
    """Runs `command` and consumes both streams at once into `spill`, in arrival order (as a
    terminal shows them). The deadline covers the start too. At the deadline the answer is
    timeout, whatever the process does next: terminate is asked for in the background and
    never awaited, so a stop that can't finish can't hold the exec, and a process the stop
    killed never reads as a normal exit."""
    out, err = _Preview(limits), _Preview(limits)
    deadline = None if command.timeout_ms is None else command.timeout_ms / 1000
    try:
        async with asyncio.timeout(deadline):
            started = await _start(session, command, context)
            if isinstance(started, Err):
                await spill.discard()
                return started
            await asyncio.gather(
                _drain(started.value.stdout, out, spill), _drain(started.value.stderr, err, spill)
            )
            code = await started.value.exit_code
    except TimeoutError:
        await spill.discard()
        stop = asyncio.create_task(_terminate(session, command.process_key, context))
        _stopping.add(stop)
        stop.add_done_callback(_stopping.discard)
        return Err(SandboxError("timeout", f"exec timed out after {command.timeout_ms} ms"))
    truncated = out.truncated or err.truncated
    full = None
    if truncated:
        sha = await spill.commit()
        full = ArtifactRef(sha256=sha, bytes=spill.written, media_type="application/octet-stream")
    else:
        await spill.discard()
    return Ok(ExecResult(code, out.text(), err.text(), truncated, full))


async def _start(
    session: SandboxSession, command: Command, context: SandboxContext
) -> Ok[ExecOutput] | Err[SandboxError]:
    return await session.exec(
        command.argv,
        context,
        process_key=command.process_key,
        cwd=command.cwd,
        env=command.env,
        timeout_ms=command.timeout_ms,
        stdin=command.stdin,
    )


async def _terminate(session: SandboxSession, process_key: str, context: SandboxContext) -> object:
    # Its answer settles nothing here: the effect is already effect_unknown and parks.
    return await session.terminate(process_key, context)


async def _drain(stream: AsyncIterator[bytes], preview: _Preview, spill: Sink) -> None:
    async for chunk in stream:
        preview.add(chunk)
        await spill.write(chunk)
