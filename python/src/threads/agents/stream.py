"""spec/api.json `RunStream`: a run's committed events as they are appended, then its result."""

import asyncio
from collections.abc import AsyncIterator
from typing import Final

from threads.agents.results import RunResult, StreamEvent


class RunStream[O]:
    """spec/api.json `RunStream`: the run's committed events as they are appended, then its
    result. A subscription to the log, not a second loop."""

    def __init__(
        self, queue: asyncio.Queue[StreamEvent | None], task: asyncio.Task[RunResult[O]]
    ) -> None:
        """`queue` receives the run's items; the run is done when its task is."""
        self._queue: Final = queue
        self._task: Final = task
        task.add_done_callback(lambda _: queue.put_nowait(None))

    @property
    def result(self) -> asyncio.Task[RunResult[O]]:
        """Await it for the `RunResult`."""
        return self._task

    def __aiter__(self) -> AsyncIterator[StreamEvent]:
        return self._items()

    async def _items(self) -> AsyncIterator[StreamEvent]:
        while (item := await self._queue.get()) is not None:
            yield item
