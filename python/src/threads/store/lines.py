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
from threads.result import Err, Ok
from threads.store.verify import StoredEvent


@dataclass(frozen=True, slots=True)
class Draft:
    """What a caller appends: the writer adds seq, ids, epoch, time and the chain."""

    type: str
    data: Mapping[str, JsonValue]
    actor: Mapping[str, JsonValue] = field(default_factory=lambda: {"kind": "host"})
    critical: bool = True
    """Pinned per known type by the schema; a mismatch fails the line's schema."""


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


def event_line(draft: Draft, at: Position) -> Ok[tuple[StoredEvent, bytes]] | Err[ParseError]:
    """The draft as its stored line, parsed back through the reader's own boundary so the
    writer can never store a line a reader would refuse."""
    value: JsonValue = {
        "actor": dict(draft.actor),
        "branch_id": at.branch_id,
        "critical": draft.critical,
        "data": dict(draft.data),
        "epoch": at.epoch,
        "event_id": uuid7(at.now),
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
    parsed = parse_log_line(text.value)
    if isinstance(parsed, Err):
        return parsed
    event = parsed.value
    if isinstance(event, Header | Head):
        raise AssertionError("an event draft parsed as a framing line")
    return Ok((event, text.value.encode("utf-8")))
