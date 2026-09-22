"""Reads a branch export: the storage trust boundary (spec/conformance/README.md, `reduce` step 1).

Per line, in order: UTF-8 and strict JSON, RFC 8785 admission, format admission, the line schema
and the critical rule (`parse_log_line`); then `seq`, `prev_hash` and the fork link; then
`validate_next`. The head checkpoint is checked last. The first failure ends the read.

A final chunk without its newline is a torn tail (an interrupted copy): it is dropped, the
valid prefix is served, and the head is flagged unverified. SQLite storage can't tear.
"""

from dataclasses import dataclass, field

from threads.log import (
    BranchId,
    Event,
    ForkEvent,
    Head,
    Header,
    LogLine,
    ParseError,
    UnknownEvent,
    parse_log_line,
)
from threads.log.digest import sha256_hex
from threads.reduce import Fold, HeadRef, ReducedState, apply, enter_segment, reduced_state
from threads.result import Err, Ok

type StoredEvent = Event | UnknownEvent


@dataclass(frozen=True, slots=True)
class Segment:
    """One branch's own lines inside an export: its header, then its events."""

    header: Header
    header_line: bytes
    events: tuple[tuple[StoredEvent, bytes], ...]
    fork_at_seq: int | None
    """The parent line this segment forks from; None for the root segment."""

    @property
    def last_line(self) -> bytes:
        return self.events[-1][1] if self.events else self.header_line


@dataclass(frozen=True, slots=True)
class VerifiedLog:
    segments: tuple[Segment, ...]
    fold: Fold
    """The fold of the whole resolved chain. Owned by this value: don't share it."""
    head: HeadRef
    head_verified: bool
    """False when the export has no head checkpoint line, or its tail was torn."""
    committed_bytes: int
    """Bytes of the header and event lines readers serve (the head line excluded)."""
    dropped: bytes
    """A torn tail's bytes, else empty."""

    @property
    def state(self) -> ReducedState:
        return reduced_state(self.fold, self.head)


@dataclass(frozen=True, slots=True)
class _Link:
    """What a child segment's fork event must name: the parent line at the fork point."""

    at_seq: int
    parent: BranchId
    at_hash: str


@dataclass(slots=True)
class _Open:
    header: Header
    header_line: bytes
    link: _Link | None
    events: list[tuple[StoredEvent, bytes]] = field(default_factory=list[tuple[StoredEvent, bytes]])

    def last_line(self) -> bytes:
        return self.events[-1][1] if self.events else self.header_line

    def awaits_fork(self) -> bool:
        return self.link is not None and not self.events

    def close(self) -> Segment:
        at_seq = None if self.link is None else self.link.at_seq
        return Segment(self.header, self.header_line, tuple(self.events), at_seq)


@dataclass(slots=True)
class _Reader:
    fold: Fold
    segments: list[_Open] = field(default_factory=list[_Open])
    head: Head | None = None
    committed: int = 0

    def line(self, raw: bytes) -> ParseError | None:
        result = _parse(raw, self._position())
        if isinstance(result, Err):
            return result.error
        parsed = result.value
        if self.head is not None:
            seq = 0 if isinstance(parsed, Header) else parsed.seq
            return ParseError("invalid_line", "the head checkpoint must be the last line", seq)
        if isinstance(parsed, Head):
            self.head = parsed
            return None
        self.committed += len(raw) + 1
        if isinstance(parsed, Header):
            return self._header(parsed, raw)
        return self._event(parsed, raw)

    def _position(self) -> int:
        # An unreadable line is named by its position: 0 first, else the last seq plus 1.
        return self.fold.seq + 1 if self.segments else 0

    def _header(self, header: Header, raw: bytes) -> ParseError | None:
        if not self.segments:
            self.segments.append(_Open(header, raw, None))
        else:
            parent = self.segments[-1]
            if parent.awaits_fork():
                return _missing_fork(self.fold.seq + 1)
            link = _Link(self.fold.seq, parent.header.branch_id, sha256_hex(parent.last_line()))
            self.segments.append(_Open(header, raw, link))
        enter_segment(self.fold, header)
        return None

    def _event(self, event: StoredEvent, raw: bytes) -> ParseError | None:
        if not self.segments:
            return ParseError("invalid_transition", "an event before any header", event.seq)
        if event.seq != self.fold.seq + 1:
            message = f"seq {event.seq} does not follow {self.fold.seq}"
            return ParseError("seq_mismatch", message, event.seq)
        segment = self.segments[-1]
        if event.prev_hash != sha256_hex(segment.last_line()):
            return ParseError("prev_hash_mismatch", "prev_hash breaks the chain", event.seq)
        error = _fork_link_error(segment, event) or apply(self.fold, event)
        if error is not None:
            return error
        segment.events.append((event, raw))
        return None

    def finish(self, dropped: bytes) -> Ok[VerifiedLog] | Err[ParseError]:
        if not self.segments:
            return Err(ParseError("invalid_line", "the log has no header", 0))
        last = self.segments[-1]
        if last.awaits_fork():
            return Err(_missing_fork(self.fold.seq + 1))
        head = HeadRef(self.fold.seq, sha256_hex(last.last_line()))
        if self.head is not None and not _matches(self.head, last, head):
            message = "the head checkpoint does not match the last line"
            return Err(ParseError("head_mismatch", message, self.head.seq))
        verified = self.head is not None and not dropped
        segments = tuple(s.close() for s in self.segments)
        return Ok(VerifiedLog(segments, self.fold, head, verified, self.committed, dropped))


def _parse(raw: bytes, position: int) -> Ok[LogLine] | Err[ParseError]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return Err(ParseError("invalid_line", "a line is not UTF-8", position))
    match parse_log_line(text):
        case Err(error=error) if error.seq is None:
            return Err(ParseError(error.code, error.message, position))
        case result:
            return result


def _fork_link_error(segment: _Open, event: StoredEvent) -> ParseError | None:
    link = segment.link if segment.awaits_fork() else None
    if not isinstance(event, ForkEvent):
        return None if link is None else _missing_fork(event.seq)
    if link is None:
        return ParseError("invalid_transition", "fork only opens a child segment", event.seq)
    if (event.data.parent_branch_id, event.data.at_hash) != (link.parent, link.at_hash):
        message = "fork does not name the parent's line at at_seq"
        return ParseError("prev_hash_mismatch", message, event.seq)
    return None


def _missing_fork(seq: int) -> ParseError:
    return ParseError("invalid_transition", "a child segment must open with its fork", seq)


def _matches(head: Head, last: _Open, ref: HeadRef) -> bool:
    return head.branch_id == last.header.branch_id and (head.seq, head.hash) == (ref.seq, ref.hash)


def verify_export(data: bytes, now: int) -> Ok[VerifiedLog] | Err[ParseError]:
    """Verifies a JSONL branch export and folds its resolved chain. `now` is the injected clock.

    Expected failures are values: the first line that fails names its pinned error code and seq.
    """
    cut = data.rfind(b"\n") + 1
    body, dropped = data[:cut], data[cut:]
    reader = _Reader(Fold(now))
    for raw in body.split(b"\n")[:-1]:
        error = reader.line(raw)
        if error is not None:
            return Err(error)
    return reader.finish(dropped)
