"""One UI connection's end of the live hub: the deltas it has not handled yet, and a wait that
ends at the next delta or append of this process, or after a poll interval for everything
another process appends. A connection without live text (the cursor route) only polls."""

import asyncio
import contextlib
from typing import Final

from threads.host.ui.hub import LiveHub
from threads.host.ui.live import Delta

POLL_S: Final = 0.05


class LiveListener:
    def __init__(self, hub: LiveHub | None, thread: str) -> None:
        """Registers now: call it before reading the log's head, and before starting the run."""
        self._inbox: list[Delta] = []
        self._wake = asyncio.Event()
        if hub is None:
            self.missed: frozenset[str] | None = None
            self._stop = lambda: None
            return
        registered = hub.listen(thread, self._heard)
        self.missed = registered.missed
        self._stop = registered.stop

    def _heard(self, delta: Delta | None) -> None:
        if delta is not None:
            self._inbox.append(delta)
        self._wake.set()

    def take(self) -> list[Delta]:
        """The deltas received since the last call, in order."""
        taken, self._inbox = self._inbox, []
        return taken

    async def next(self) -> None:
        """Until something may have changed."""
        if self._inbox:
            return
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), POLL_S)

    def stop(self) -> None:
        self._stop()
