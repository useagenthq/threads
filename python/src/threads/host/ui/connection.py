"""One UI connection's frames (spec/schema/ui/README.md): the run's committed events in log
order, each as its frames with ids `<seq>:<k>`, the frames at or before a cursor left out, live
text for the parts this connection may stream, and the closing frames. It reads nothing but what
it is given, so a replay of the log gives the same frames."""

from collections.abc import Sequence, Set
from dataclasses import dataclass

from threads.host.ui.ag_ui import ag_ui_events
from threads.host.ui.ai_sdk import ai_sdk_chunks
from threads.host.ui.closing import Ending, RunIds, closing, text_end
from threads.host.ui.facts import RunFacts
from threads.host.ui.frame import Frame, Protocol, bare
from threads.host.ui.framed import Written, part_id, shown_parts
from threads.host.ui.live import Delta, LiveParts, Mismatch
from threads.log import (
    Event,
    ModelAttemptAbandonedEvent,
    ModelRequestEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
)

INF = float("inf")


@dataclass(frozen=True, slots=True)
class Cursor:
    """A frame position: frames of event `seq` up to index `k` (inf: all of them)."""

    seq: float
    k: float


@dataclass(frozen=True, slots=True)
class Step:
    """A step's frames; `broken` names a live part whose commit contradicts it: close, no
    [DONE]."""

    frames: Sequence[Frame]
    broken: str | None = None


def event_frames(protocol: Protocol, e: Event, facts: RunFacts) -> list[Frame]:
    """A committed event's canonical frames, given the run's events before it; then it is one
    of them."""
    chunks = ai_sdk_chunks(e, facts) if protocol == "ai-sdk" else ag_ui_events(e, facts)
    facts.add(e)
    return [Frame(data, f"{e.seq}:{k}") for k, data in enumerate(chunks)]


class UiConnection:
    def __init__(
        self, protocol: Protocol, after: Cursor | None = None, missed: Set[str] | None = None
    ) -> None:
        self.facts: RunFacts = RunFacts()
        self._protocol: Protocol = protocol
        self._after: Cursor | None = after
        self._live: LiveParts | None = None if missed is None else LiveParts(protocol, missed)
        self._requests: set[str] = set()
        """Turn requests whose model_request this connection has passed."""
        self._settled: set[str] = set()
        """Requests committed or abandoned: their late deltas are dropped."""
        self._queued: dict[str, list[Delta]] = {}
        """Deltas of a request whose model_request this connection hasn't read yet."""

    def event(self, e: Event) -> Step:
        """The frames of the next committed event of the run."""
        every = event_frames(self._protocol, e, self.facts)
        frames = [f for k, f in enumerate(every) if self._sends(e.seq, k)]
        live = self._live
        if live is None:
            return Step(frames)
        match e:
            case ModelRequestEvent():
                return Step(self._requested(e, live, frames))
            case ModelAttemptAbandonedEvent(data=data):
                self._settled.add(data.request_event_id)
                ends = [
                    Frame(text_end(self._protocol, p)) for p in live.settle(data.request_event_id)
                ]
                return Step([*ends, *frames])
            case ModelResponseEvent() | ModelResponseRecoveredEvent():
                return self._committed(e, live, frames)
            case _:
                return Step(frames)

    def _requested(self, e: ModelRequestEvent, live: LiveParts, frames: list[Frame]) -> list[Frame]:
        # A compaction side request's text never shows, live or committed.
        if e.data.purpose == "compaction":
            self._settled.add(e.event_id)
            return frames
        self._requests.add(e.event_id)
        queued = self._queued.pop(e.event_id, [])
        return [*frames, *(f for d in queued for f in live.delta(d))]

    def _committed(
        self,
        e: ModelResponseEvent | ModelResponseRecoveredEvent,
        live: LiveParts,
        frames: list[Frame],
    ) -> Step:
        request = e.data.request_event_id
        self._settled.add(request)
        texts = {
            part_id(request, p.index): p.text
            for p in shown_parts(e)
            if isinstance(p, Written) and p.kind == "text"
        }
        opened = [p for p in live.open() if p.startswith(f"{request}:")]
        out = live.substitute(request, frames, texts)
        if isinstance(out, Mismatch):
            return Step([Frame(text_end(self._protocol, p)) for p in opened], out.part)
        return Step(out)

    def delta(self, d: Delta) -> list[Frame]:
        """The frames one live delta adds on this connection, if any."""
        if self._live is None or d.request_id in self._settled:
            return []
        if d.request_id in self._requests:
            return self._live.delta(d)
        self._queued.setdefault(d.request_id, []).append(d)
        return []

    def close(self, end: Ending, ids: RunIds) -> list[Frame]:
        """The closing frames once the run has ended (or parked)."""
        opened = [] if self._live is None else self._live.open()
        return bare(closing(self._protocol, end, self.facts, opened, ids))

    def _sends(self, seq: int, k: int) -> bool:
        after = self._after
        return after is None or seq > after.seq or (seq == after.seq and k > after.k)
