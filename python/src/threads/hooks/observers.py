"""Observers: committed events delivered after append, asynchronously and in
log order. They can lag and fail without touching execution or the log: each keeps a durable
cursor, advanced only after its handler returned, so a failed or interrupted delivery is retried
from the cursor on the next poke or the next run."""

import asyncio
import contextlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from threads.hooks.extension import Observer
from threads.log import BranchId, Event
from threads.store.cursors import ObserverCursors


@dataclass(slots=True)
class _Lane:
    name: str
    on: Mapping[str, Observer]
    running: "asyncio.Task[None] | None" = None
    again: bool = False


class ObserverPump:
    def __init__(
        self,
        cursors: ObserverCursors,
        branch: BranchId,
        events: Callable[[], Sequence[Event]],
        observers: Mapping[str, Mapping[str, Observer]],
    ) -> None:
        self._cursors = cursors
        self._branch = branch
        self._events = events
        self._lanes = [_Lane(name, on) for name, on in observers.items() if on]

    def poke(self) -> None:
        """New events may be committed: every idle observer starts catching up. Never blocks."""
        for lane in self._lanes:
            if lane.running is not None:
                lane.again = True
                continue
            lane.running = asyncio.get_running_loop().create_task(self._drain(lane))

    async def idle(self) -> None:
        """Every observer has caught up, or failed and stopped until the next poke."""
        for lane in self._lanes:
            if lane.running is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await lane.running

    async def _drain(self, lane: _Lane) -> None:
        try:
            while True:
                lane.again = False
                await self._deliver(lane)
                if not lane.again:
                    return
        except Exception:  # noqa: S110 - a failed handler leaves its cursor; the next poke retries
            pass
        finally:
            lane.running = None

    async def _deliver(self, lane: _Lane) -> None:
        cursor = await self._cursors.get(lane.name, self._branch)
        for event in [e for e in self._events() if e.seq > cursor]:
            handler = lane.on.get(event.type) or lane.on.get("*")
            if handler is not None:
                # Events are frozen: an observer can't change the one the loop holds.
                await handler(event)
            await self._cursors.advance(lane.name, self._branch, event.seq)
