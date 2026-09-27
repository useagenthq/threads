"""Derived ids: a name that is a function of its fields, with no lookup table and nothing recorded.
One implementation, because the UI's chat key (spec/schema/ui/README.md), an inbound A2A context
and an outbound A2A message id (spec/schema/a2a/) derive the same way under different domain
strings, and a byte of difference between them would let one surface reach the other's thread.
Below the host, because the exposed side and the client side both use it."""

import hashlib
import uuid
from collections.abc import Sequence

from threadsai.log import ThreadId


def _lp(text: str) -> bytes:
    """4-byte big-endian UTF-8 length, then the bytes: no two field splits collide."""
    data = text.encode()
    return len(data).to_bytes(4, "big") + data


def _uuidv8(digest: bytes) -> str:
    """A sha256 digest's first 16 bytes as a UUIDv8: version 8 and the RFC 4122 variant written
    over them, so the result is a valid UUID no version-7 generator can produce."""
    raw = bytearray(digest[:16])
    raw[6] = (raw[6] & 0x0F) | 0x80
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def derived_id(domain: str, fields: Sequence[str]) -> str:
    """`UUIDv8 of sha256(lp(domain) || lp(field) || ...)`. The domain string is what keeps two
    surfaces' derivations apart: the same fields under another domain name a different id."""
    parts = (domain, *fields)
    return _uuidv8(hashlib.sha256(b"".join(_lp(f) for f in parts)).digest())


def derived_thread_id(domain: str, fields: Sequence[str]) -> ThreadId:
    """`derived_id` as a thread id: what a chat key and an A2A context each name."""
    return ThreadId(derived_id(domain, fields))


def derived_uuid(domain: str, fields: Sequence[str]) -> str:
    """`UUIDv8 of sha256(domain || field || ...)`, with no length prefix: for fixed-width
    fields only."""
    return _uuidv8(hashlib.sha256("".join((domain, *fields)).encode()).digest())
