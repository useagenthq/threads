"""One Docker exec: created, started, and its multiplexed stream split into the two byte
streams and the exit code the protocol returns (`ExecOutput`).

The stream is the container's own output, so it is a trust boundary: its 8-byte frame headers
are parsed strictly and a frame over the cap is refused, both as a typed `unavailable`.
"""

import asyncio
from collections.abc import Mapping, Sequence
from typing import Final, Literal

from pydantic import JsonValue

from threads.adapters.sandboxes.docker import records
from threads.adapters.sandboxes.docker.engine import Engine, argv_env
from threads.adapters.sandboxes.streams import Pipe, pump
from threads.sandbox.protocol import ExecOutput

HEADER: Final = 8
FRAME_MAX: Final = 64 * 1024 * 1024
_STREAMS: Final[Mapping[int, Literal["stdout", "stderr"]]] = {1: "stdout", 2: "stderr"}
_EXIT_POLLS: Final = 20
_EXIT_PAUSE_S: Final = 0.05

type Chunk = tuple[Literal["stdout", "stderr"], bytes]


class FrameError(Exception):
    """The daemon's multiplexed stream broke its own framing."""


class Frames:
    """Docker's stream multiplexer, fed arbitrary splits: `[stream, 0, 0, 0, len:u32be]`."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[Chunk]:
        self._buf += chunk
        out: list[Chunk] = []
        while len(self._buf) >= HEADER:
            head = bytes(self._buf[:HEADER])
            stream = _STREAMS.get(head[0])
            if stream is None or head[1:4] != b"\0\0\0":
                raise FrameError(f"unknown stream frame header {bytes(head).hex()}")
            size = int.from_bytes(head[4:HEADER], "big")
            if size > FRAME_MAX:
                raise FrameError(f"a {size}-byte frame is over the {FRAME_MAX}-byte cap")
            if len(self._buf) < HEADER + size:
                return out
            out.append((stream, bytes(self._buf[HEADER : HEADER + size])))
            del self._buf[: HEADER + size]
        return out

    def end(self) -> None:
        """Raises when the stream stopped inside a frame."""
        if self._buf:
            raise FrameError(f"the stream ended {len(self._buf)} bytes into a frame")


def body(
    cmd: Sequence[str],
    env: Mapping[str, str],
    cwd: str,
    user: str,
) -> Mapping[str, JsonValue]:
    return {
        "AttachStdout": True,
        "AttachStderr": True,
        "AttachStdin": False,
        "Tty": False,
        "User": user,
        "Env": list(argv_env(env)),
        "WorkingDir": cwd,
        "Cmd": list(cmd),
    }


async def start(
    engine: Engine,
    container: str,
    spec: Mapping[str, JsonValue],
    supervised: str | None = None,
) -> ExecOutput:
    """Starts the exec and returns once the daemon accepted it; its output is pumped from the
    stream, which the pump closes.

    `supervised` is the key hash of a command running through the supervisor, whose own exit
    code is in its record rather than in the exec's status (records.py `exit_code`)."""
    made = await engine.exec_create(container, spec)
    stream = await engine.exec_start(made.id)
    pipe = Pipe()

    async def feed(into: Pipe) -> int:
        frames = Frames()
        try:
            async for chunk in stream.aiter_raw():
                for kind, data in frames.feed(chunk):
                    await (into.stdout(data) if kind == "stdout" else into.stderr(data))
            frames.end()
        finally:
            await stream.aclose()
        status = await _exit_code(engine, made.id)
        if supervised is None:
            return status
        state = await records.read_state(engine, container)
        return records.exit_code(state, supervised, status)

    engine.pumps.add(task := pump(pipe, feed))
    task.add_done_callback(engine.pumps.discard)
    return pipe.output()


async def run_to_end(
    engine: Engine, container: str, cmd: Sequence[str], user: str
) -> tuple[int, bytes, bytes]:
    """A short fixed command of the adapter's own (the probe, `--check`): its whole output."""
    output = await start(engine, container, body(cmd, {}, "/", user))
    out = b"".join([c async for c in output.stdout])
    err = b"".join([c async for c in output.stderr])
    return await output.exit_code, out, err


async def _exit_code(engine: Engine, exec_id: str) -> int:
    """The daemon sets `ExitCode` when it reaps the process, which can trail the stream's end
    by a moment."""
    for _ in range(_EXIT_POLLS):
        state = await engine.exec_state(exec_id)
        if not state.running and state.exit_code is not None:
            return state.exit_code
        await asyncio.sleep(_EXIT_PAUSE_S)
    raise FrameError(f"exec {exec_id} reported no exit code after its stream ended")
