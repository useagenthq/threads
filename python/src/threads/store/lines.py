"""Builds new canonical lines for the writer: branch headers and events (wire rules 7 and 9)."""

import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import JsonValue

from threads import VERSION
from threads.log import (
    BranchId,
    Head,
    Header,
    ParseError,
    ThreadId,
    parse_log_line,
)
from threads.log.digest import sha256_hex
from threads.log.jcs import canonicalize
from threads.redaction import contains_secret, redact_json
from threads.result import Err, Ok
from threads.store.verify import StoredEvent, VerifiedLog


@dataclass(frozen=True, slots=True)
class Draft:
    """What a caller appends: the writer adds seq, ids, epoch, time and the chain."""

    type: str
    data: Mapping[str, JsonValue]
    actor: Mapping[str, JsonValue] = field(default_factory=lambda: {"kind": "host"})
    critical: bool = True
    """Pinned per known type by the schema; a mismatch fails the line's schema."""
    event_id: str | None = None
    """Set when a later draft of the same append must name this event (a resumed's cause, a
    user_input's delivery); otherwise the writer mints a UUIDv7."""


@dataclass(frozen=True, slots=True)
class Position:
    """Where the next event goes: its branch, thread, seq, epoch, chain link and clock."""

    thread_id: ThreadId
    branch_id: BranchId
    seq: int
    epoch: int
    prev_line: bytes
    now: int


def uuid7(now_ms: int) -> str:
    """A UUIDv7 (RFC 9562): 48-bit ms timestamp, version 7, variant 10, 74 random bits."""
    rand = secrets.randbits(74)
    value = (now_ms & (1 << 48) - 1) << 80 | 0x7 << 76 | (rand >> 62) << 64 | 0b10 << 62
    return str(uuid.UUID(int=value | rand & (1 << 62) - 1))


def header_line(thread_id: ThreadId, branch_id: BranchId, now: int) -> bytes:
    header: JsonValue = {
        "branch_id": branch_id,
        "created_at": now,
        "format": "threads.log",
        "format_version": 1,
        "thread_id": thread_id,
        "writer": {"impl": "threads-py", "version": VERSION},
    }
    return _canonical(header)


def head_line(branch_id: BranchId, seq: int, head_hash: str) -> bytes:
    head: JsonValue = {
        "branch_id": branch_id,
        "format": "threads.head",
        "format_version": 1,
        "hash": head_hash,
        "seq": seq,
    }
    return _canonical(head)


def _canonical(value: JsonValue) -> bytes:
    match canonicalize(value):
        case Ok(value=text):
            return text.encode("utf-8")
        case Err(error=reason):
            raise ValueError(reason)


def stored_secret(seq: int | None = None) -> ParseError:
    """An event refused because its stored bytes would hold a registered value (C5)."""
    message = "the event's stored bytes would hold a registered secret; nothing appended"
    return ParseError("secret_in_stored_bytes", message, seq)


def event_line(
    draft: Draft, at: Position
) -> Ok[tuple[StoredEvent, bytes, bytes]] | Err[ParseError]:
    """The draft as its stored line, parsed back through the reader's own boundary so the
    writer can never store a line a reader would refuse; then the canonical bytes of its
    content (`actor` and `data`), which the store's thread checks again as it publishes."""
    # Nothing is recorded with a resolved secret in it (C5): every event passes here.
    actor, data = redact_json(dict(draft.actor)), redact_json(dict(draft.data))
    value: JsonValue = {
        "actor": actor,
        "branch_id": at.branch_id,
        "critical": draft.critical,
        "data": data,
        "epoch": at.epoch,
        "event_id": draft.event_id or uuid7(at.now),
        "prev_hash": sha256_hex(at.prev_line),
        "seq": at.seq,
        "thread_id": at.thread_id,
        "time": at.now,
        "type": draft.type,
        "type_version": 1,
    }
    text = canonicalize(value)
    if isinstance(text, Err):
        return Err(ParseError("invalid_line", text.error, at.seq))
    content = _canonical({"actor": actor, "data": data})
    if contains_secret(content):
        # Canonical escaping or JSON punctuation can still join redacted strings into a value.
        return Err(stored_secret(at.seq))
    parsed = parse_log_line(text.value)
    if isinstance(parsed, Err):
        return parsed
    event = parsed.value
    if isinstance(event, Header | Head):
        raise AssertionError("an event draft parsed as a framing line")
    return Ok((event, text.value.encode("utf-8"), content))


def imported_bytes(log: VerifiedLog) -> bytes:
    """Every byte an import stores: each segment's header and event lines, and a torn tail."""
    lines = [line for s in log.segments for line in (s.header_line, *(b for _, b in s.events))]
    return b"\n".join(lines) + b"\n" + log.dropped
