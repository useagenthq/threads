"""Where a run's stream goes: each appended event, each redacted text delta, and each wait, to
the caller's emit; appends also wake the run's observers."""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from threads.agents.definition import Definition
from threads.agents.results import DeltaItem, EventItem, StatusItem, StreamEvent
from threads.agents.store import now_ms
from threads.hooks.observers import ObserverPump
from threads.log import EventId
from threads.store import SqliteStore, StoredEvent, Writer

type Emit = Callable[[StreamEvent], None]
type OnDelta = Callable[[EventId, int, str], None]
"""Internal: each redacted delta with its part index (the host's live hub; DeltaItem has none)."""


@dataclass(frozen=True, slots=True)
class RunStream:
    emit: Emit
    pump: ObserverPump
    on_delta: OnDelta | None = None

    def delta(self, request: EventId, part: int, text: str) -> None:
        self.emit(DeltaItem(request, text))
        if self.on_delta is not None:
            self.on_delta(request, part, text)

    def observe(self, events: Sequence[StoredEvent]) -> None:
        for event in events:
            self.emit(EventItem(event))
        self.pump.poke()

    async def wait_until(self, when: int) -> None:
        self.emit(StatusItem(when))
        await asyncio.sleep(max(0, when - now_ms()) / 1000)


def run_stream[D](
    sq: SqliteStore,
    writer: Writer,
    definition: Definition[D],
    emit: Emit,
    on_delta: OnDelta | None,
) -> RunStream:
    """A run's stream, with the observer pump already poked for its branch."""
    observers = {e.name: e.on for e in definition.extensions}
    pump = ObserverPump(sq.cursors, writer.branch_id, lambda: writer.fold.events, observers)
    pump.poke()
    return RunStream(emit, pump, on_delta)
