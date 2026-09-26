"""SSE in both directions. Writing: one `data:` line per item, with an `id:` so `Last-Event-ID`
resumes exactly. Reading: a peer's stream frame by frame, with the field parsing the WHATWG
event-stream rules require (a `\\r\\n`, `\\n` or `\\r` line break; a blank line dispatches; one
leading space after the colon is dropped; several `data:` lines join with `\\n`; a `:` comment is
ignored)."""

import re
from collections.abc import AsyncIterable, AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Final, Literal


@dataclass(frozen=True, slots=True)
class SseFrame:
    data: str
    id: str | None = None
    """None when the item has no resumable position of its own."""


@dataclass(frozen=True, slots=True)
class SseEvent:
    data: str
    id: str | None


def sse_bytes(frame: SseFrame) -> bytes:
    """One frame as it goes on the wire."""
    head = "" if frame.id is None else f"id: {frame.id}\n"
    return f"{head}data: {frame.data}\n\n".encode()


async def sse_body(frames: AsyncIterable[SseFrame]) -> AsyncIterator[bytes]:
    """A stream of frames as a response body."""
    async for frame in frames:
        yield sse_bytes(frame)


_BREAK: Final = re.compile(r"\r\n|\n|\r")

type Field = tuple[Literal["data", "id", "other"], str]


def field_of(line: str) -> Field:
    """A line's field and value. A leading space after the colon belongs to the delimiter, not the
    value; a line with no colon is a field with an empty value; a line starting with `:` is a
    comment. An `id` holding a NUL is ignored, as the event-stream rules require."""
    if line.startswith(":"):
        return ("other", "")
    name, sep, raw = line.partition(":")
    value = raw[1:] if sep and raw.startswith(" ") else raw
    if name == "data":
        return ("data", value)
    if name == "id" and "\0" not in value:
        return ("id", value)
    return ("other", "")


def split(text: str, *, ended: bool) -> tuple[tuple[str, ...], str]:
    """The complete lines in `text`, and what is left over. A trailing `\\r` may be the first half
    of a `\\r\\n`, so unless the stream has ended it waits for the next chunk rather than being
    taken as a break of its own. A final piece with no line break is left over, and at the end of a
    stream it is dropped: an unterminated block was never dispatched."""
    hold_cr = not ended and text.endswith("\r")
    lines = _BREAK.split(text[:-1] if hold_cr else text)
    rest = lines.pop() + ("\r" if hold_cr else "")
    return tuple(lines), rest


@dataclass(slots=True)
class _Assembler:
    """The event-stream state machine, shared by the streaming and whole-body readers so there is
    one implementation of what an event is."""

    id: str | None = None
    data: list[str] = field(default_factory=list[str])

    def feed(self, line: str) -> SseEvent | None:
        if line == "":
            # A blank line dispatches, and only a block that carried data is an event.
            got = SseEvent("\n".join(self.data), self.id) if self.data else None
            self.data = []
            return got
        name, value = field_of(line)
        if name == "data":
            self.data.append(value)
        elif name == "id":
            self.id = value
        return None


async def sse_events(chunks: AsyncIterable[bytes]) -> AsyncIterator[SseEvent]:
    """A response body's SSE events, as its chunks arrive."""
    assembler = _Assembler()
    buffered = ""
    async for chunk in chunks:
        ready, buffered = split(buffered + chunk.decode(errors="replace"), ended=False)
        for line in ready:
            event = assembler.feed(line)
            if event is not None:
                yield event
    ready, _ = split(buffered, ended=True)
    for line in ready:
        event = assembler.feed(line)
        if event is not None:
            yield event


def sse_events_of(text: str) -> tuple[SseEvent, ...]:
    """The events in one complete body: the form for a transport that answers whole bytes."""
    assembler = _Assembler()
    ready, _ = split(text, ended=True)
    return tuple(_dispatched(assembler, ready))


def _dispatched(assembler: _Assembler, lines: Iterable[str]) -> Iterable[SseEvent]:
    for line in lines:
        event = assembler.feed(line)
        if event is not None:
            yield event
