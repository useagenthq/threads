"""One UI connection's frames over time (spec/schema/ui/README.md): the opening frame, for an
AG-UI replay the snapshot and open-state preamble at the first read, then the run's events as
each read finds them, the live deltas in between, and the closing frames. It reads nothing but
what it is handed, so the host's stream and the conformance runner drive the same code."""

from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from typing import Literal

from threads.host.outcome import run_events, run_start
from threads.host.ui.closing import Ending, RunIds
from threads.host.ui.connection import INF, Cursor, Step, UiConnection
from threads.host.ui.frame import Chunk, Frame, Protocol, bare
from threads.host.ui.live import Delta
from threads.host.ui.snapshot import messages_snapshot, preamble
from threads.log import Event, EventId


@dataclass(frozen=True, slots=True)
class SessionPlan:
    protocol: Protocol
    run_id: EventId
    ids: RunIds
    """The ids AG-UI's run events echo."""
    after: Cursor | None = None
    """Frames the client already has (AI SDK)."""
    replay: Literal["head"] | int | None = None
    """An AG-UI replay: a snapshot at the first read's head, or through a cursor's event."""
    extra: Sequence[Chunk] = ()
    """Envelope frames after the opening (and the snapshot): resume conflicts."""
    receipts: Mapping[str, str] = field(default_factory=dict[str, str])
    """The ui receipts' message ids by run id, for a snapshot's user messages."""


def own_events(events: Sequence[Event], run_id: EventId) -> Sequence[Event]:
    """The run's own events: its user_input through its end, or the latest while it goes on."""
    start = run_start(events, run_id)
    return () if start is None else run_events(events, start)


class UiSession:
    def __init__(
        self, plan: SessionPlan, events: Sequence[Event], missed: Set[str] | None = None
    ) -> None:
        """Opens on the log as the first read after registration finds it. `missed`: the live
        hub's started parts at registration; None, the connection streams no live text."""
        self._plan = plan
        own = own_events(events, plan.run_id)
        head = own[-1].seq if own else 0
        h = head if plan.replay == "head" else (plan.replay or 0)
        # A replay's events through its snapshot point are folded, never sent.
        after = plan.after if plan.replay is None else Cursor(h, INF)
        self._connection = UiConnection(plan.protocol, after, missed)
        self._seen = 0
        if plan.replay is None:
            return
        for e in own:
            if e.seq <= h:
                self._connection.event(e)
        self._seen = h

    def opening(self, events: Sequence[Event]) -> list[Frame]:
        """The opening frames; for a replay, the snapshot of `events` through its point."""
        plan = self._plan
        start: Chunk = (
            {"type": "start", "messageId": plan.run_id}
            if plan.protocol == "ai-sdk"
            else {"type": "RUN_STARTED", **plan.ids.json()}
        )
        if plan.replay is None:
            return bare([start, *plan.extra])
        history = [e for e in events if e.seq <= self._seen]
        return bare(
            [
                start,
                messages_snapshot(history, plan.receipts),
                *plan.extra,
                *preamble(self._connection.facts),
            ]
        )

    def deltas(self, deltas: Sequence[Delta]) -> list[Frame]:
        return [f for d in deltas for f in self._connection.delta(d)]

    def read(self, events: Sequence[Event]) -> Step:
        """The frames of the run's events this read finds; `broken` ends the connection."""
        frames: list[Frame] = []
        for e in own_events(events, self._plan.run_id):
            if e.seq <= self._seen:
                continue
            self._seen = e.seq
            step = self._connection.event(e)
            frames.extend(step.frames)
            if step.broken is not None:
                return Step(frames, step.broken)
        return Step(frames)

    def close(self, end: Ending) -> list[Frame]:
        return self._connection.close(end, self._plan.ids)
