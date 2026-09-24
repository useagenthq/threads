"""Live text on one connection (spec/schema/ui/README.md, "Live text"): which parts it streams
as deltas arrive, and how a part's committed frames are substituted once its response lands.
Deltas are best-effort and never logged; every frame this sends carries no SSE id."""

from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, replace

from threads.host.ui.frame import Chunk, Frame, Protocol
from threads.host.ui.framed import part_id

_STARTS = ("text-start", "TEXT_MESSAGE_START")
_CONTENTS = ("text-delta", "TEXT_MESSAGE_CONTENT")


@dataclass(frozen=True, slots=True)
class Delta:
    request_id: str
    part: int
    text: str


@dataclass(frozen=True, slots=True)
class Mismatch:
    """A live part whose commit contradicts what was streamed: the connection must close."""

    part: str


class LiveParts:
    def __init__(self, protocol: Protocol, missed: Set[str]) -> None:
        self._protocol: Protocol = protocol
        self._missed = missed
        """Parts the hub had seen a delta for when this connection registered: never attached."""
        self._sent: dict[str, str] = {}
        """Text sent live per open part id, in the order the parts opened."""
        self._refused: set[str] = set()
        self._stopped: set[str] = set()
        """Requests whose live streaming stopped on this connection (the order rule)."""

    def delta(self, d: Delta) -> list[Frame]:
        """The frames one delta adds: a part opens only at its first delta, in model order."""
        pid = part_id(d.request_id, d.part)
        sent = self._sent.get(pid)
        if sent is not None:
            self._sent[pid] = sent + d.text
            return [Frame(self._text(pid, d.text))]
        if pid in self._missed or pid in self._refused:
            return []
        if not self._attaches(d):
            self._refused.add(pid)
            self._stopped.add(d.request_id)
            return []
        self._sent[pid] = d.text
        return [Frame(self._start(pid)), Frame(self._text(pid, d.text))]

    def _attaches(self, d: Delta) -> bool:
        """Part i streams only while every part before it streamed here."""
        if d.request_id in self._stopped:
            return False
        return d.part == 0 or part_id(d.request_id, d.part - 1) in self._sent

    def settle(self, request_id: str) -> list[str]:
        """Closes a request's live parts: at its commit or its abandonment."""
        opened = [p for p in self._sent if p.startswith(f"{request_id}:")]
        for p in opened:
            del self._sent[p]
        self._stopped.discard(request_id)
        return opened

    def open(self) -> list[str]:
        """Every part still open live, in the order they opened."""
        return list(self._sent)

    def substitute(
        self, request_id: str, frames: Sequence[Frame], text_at: Mapping[str, str]
    ) -> list[Frame] | Mismatch:
        """The canonical frames of a committed response with this connection's live parts
        substituted: a live part skips its start and gets only the rest of its text."""
        for pid, sent in self._sent.items():
            committed = text_at.get(pid)
            if pid.startswith(f"{request_id}:") and not (
                committed is not None and committed.startswith(sent)
            ):
                return Mismatch(pid)
        out: list[Frame] = []
        for f in frames:
            pid = self._part_of(f.data)
            sent = None if pid is None else self._sent.get(pid)
            if pid is None or sent is None or f.data.get("type") not in _STARTS + _CONTENTS:
                out.append(f)
            elif f.data.get("type") in _CONTENTS:
                rest = text_at.get(pid, "")[len(sent) :]
                if rest:
                    out.append(replace(f, data=self._text(pid, rest)))
        self.settle(request_id)
        return out

    def _start(self, pid: str) -> Chunk:
        if self._protocol == "ai-sdk":
            return {"type": "text-start", "id": pid}
        return {"type": "TEXT_MESSAGE_START", "messageId": pid, "role": "assistant"}

    def _text(self, pid: str, delta: str) -> Chunk:
        if self._protocol == "ai-sdk":
            return {"type": "text-delta", "id": pid, "delta": delta}
        return {"type": "TEXT_MESSAGE_CONTENT", "messageId": pid, "delta": delta}

    def _part_of(self, data: Chunk) -> str | None:
        """The part a text start or content frame belongs to; None for any other frame."""
        if data.get("type") not in _STARTS + _CONTENTS:
            return None
        value = data.get("id" if self._protocol == "ai-sdk" else "messageId")
        return value if isinstance(value, str) else None
