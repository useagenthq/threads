"""Shared by the team deadline tests: a clock the test moves, a member held at its first request,
and polling until a condition holds. Mirrors TypeScript's test/team/clock-kit.ts."""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import override

import pytest
from pydantic import JsonValue
from team.run_kit import sq_of

from threads import Store, scripted_model
from threads.loop.model import ModelChunk, ModelContext, ModelRequest
from threads.loop.scripted import ScriptedModel


class Held(ScriptedModel):
    """A scripted model whose first request waits for `release`."""

    def __init__(self, responses: Sequence[JsonValue], release: asyncio.Event) -> None:
        base = scripted_model({"responses": list(responses)})
        super().__init__(base._entries, base._lookups)
        self._release = release
        self._first = True

    @override
    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        if self._first:
            self._first = False
            await self._release.wait()
        async for chunk in super().send(request, context):
            yield chunk


def elapsing(monkeypatch: pytest.MonkeyPatch) -> Callable[[Store, int], Awaitable[None]]:
    """Moves the clock on by `ms`, renewing every live lease as each holder's timer would."""
    offset = [0]
    real = time.time_ns
    monkeypatch.setattr(time, "time_ns", lambda: real() + offset[0] * 1_000_000)

    async def elapse(store: Store, ms: int) -> None:
        sq = await sq_of(store)
        now = time.time_ns() // 1_000_000
        await sq.run(
            lambda c: c.execute(
                "UPDATE leases SET expires_at = expires_at + ? WHERE expires_at > ?", (ms, now)
            )
        )
        offset[0] += ms

    return elapse


async def until(check: Callable[[], Awaitable[bool]]) -> None:
    for _ in range(500):
        if await check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting")


async def count(store: Store, sql: str) -> int:
    sq = await sq_of(store)
    rows: list[tuple[int]] = await sq.run(lambda c: c.execute(sql).fetchall())
    return rows[0][0]
