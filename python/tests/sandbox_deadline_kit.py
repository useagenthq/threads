"""An exec deadline over a real adapter: at the deadline the sandbox layer answers
timeout, whether the best-effort stop finishes, never finishes, or kills the process so its
stream ends with 137 — a killed process never reads as a normal exit, and a stop that hangs
never holds the exec."""

import asyncio
from collections.abc import Mapping, Sequence

from sandbox_contract import Check, Harness, session
from sandbox_kit import OPEN

from threads.log import SnapshotData, Spill
from threads.loop.tools import Termination
from threads.result import Err, Ok
from threads.sandbox import Command, SandboxSession, run_exec
from threads.sandbox.protocol import NO_ENV, ExecOutput, SandboxContext, SandboxError, SandboxId
from threads.store import SqliteStore

LIMITS = Spill(threshold_bytes=100, head_bytes=10, tail_bytes=5, request_budget_bytes=1000)
_DEADLINE_MS = 50
_PROMPT_S = 2.0
"""Well past the deadline, well short of a hang."""


class HangingStop:
    """The adapter's session, except that terminate never finishes."""

    def __init__(self, inner: SandboxSession) -> None:
        self._inner = inner
        self.stops = 0

    @property
    def id(self) -> SandboxId:
        return self._inner.id

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
        return await self._inner.exec(
            command, context, process_key=process_key, cwd=cwd, env=env, stdin=stdin
        )

    async def terminate(
        self, process_key: str, context: SandboxContext
    ) -> Ok[Termination] | Err[SandboxError]:
        self.stops += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def upload(
        self, path: str, data: bytes, context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]:
        return await self._inner.upload(path, data, context)

    async def download(self, path: str, context: SandboxContext) -> Ok[bytes] | Err[SandboxError]:
        return await self._inner.download(path, context)

    async def snapshot(
        self, operation_key: str, context: SandboxContext
    ) -> Ok[SnapshotData] | Err[SandboxError]:
        return await self._inner.snapshot(operation_key, context)

    async def close(self, context: SandboxContext) -> Ok[None] | Err[SandboxError]:
        return await self._inner.close(context)


async def _deadline(s: SandboxSession) -> Ok[object] | Err[SandboxError]:
    opened = await SqliteStore.open()
    assert isinstance(opened, Ok)
    try:
        command = Command(["sleep", "100"], "slow", timeout_ms=_DEADLINE_MS)
        spill = await opened.value.spill()
        async with asyncio.timeout(_PROMPT_S):
            return await run_exec(s, command, OPEN, spill, LIMITS)
    finally:
        await opened.value.close()


async def a_deadline_answers_timeout_though_the_stop_kills(h: Harness) -> None:
    """The provider's stop ends the stream with 137 after the deadline: still timeout."""
    got = await _deadline(await session(h))
    assert isinstance(got, Err)
    assert got.error.code == "timeout"


async def a_deadline_answers_timeout_though_the_stop_hangs(h: Harness) -> None:
    hanging = HangingStop(await session(h))
    got = await _deadline(hanging)
    assert isinstance(got, Err)
    assert got.error.code == "timeout"
    await asyncio.sleep(0)
    assert hanging.stops == 1, "terminate is still asked for, in the background"


DEADLINE: tuple[Check, ...] = (
    a_deadline_answers_timeout_though_the_stop_kills,
    a_deadline_answers_timeout_though_the_stop_hangs,
)
