"""An attempt's text deltas as a streaming caller sees them: redacted per part before they go
out, never logged. Each part has its own redactor, so a part's live text is always a prefix of
its committed text; a part's held tail goes out once a later part begins, or at the end."""

from collections.abc import Callable
from typing import TYPE_CHECKING

from threads.log import EventId
from threads.redaction import text_stream

if TYPE_CHECKING:
    from threads.redaction.scan import Stream


class Shown:
    def __init__(self, request: EventId, send: Callable[[EventId, int, str], None]) -> None:
        self._request = request
        self._send = send
        self._parts: dict[int, Stream] = {}
        self._done = -1
        """Parts whose tail went out: a later delta for one is dropped, never shown raw."""

    def feed(self, part: int, text: str) -> None:
        if part <= self._done:
            return
        self._flush(lambda p: p < part)
        self._done = part - 1
        stream = self._parts.setdefault(part, text_stream())
        self._emit(part, stream.feed(text))

    def end(self) -> None:
        """The held tails, once the response is complete."""
        self._flush(lambda _p: True)

    def _flush(self, which: Callable[[int], bool]) -> None:
        for part in [p for p in self._parts if which(p)]:
            self._emit(part, self._parts.pop(part).end())

    def _emit(self, part: int, text: str) -> None:
        if text:
            self._send(self._request, part, text)
