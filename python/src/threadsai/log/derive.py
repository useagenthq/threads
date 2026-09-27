"""Derived ids: a UUIDv8 over length-prefixed fields, so every process names the same row with
no lookup table (the chat key, spec/schema/ui/README.md; a host team's ids, "Teams Phase 2")."""

import hashlib
import uuid


def lp(text: str) -> bytes:
    """4-byte big-endian UTF-8 length, then the bytes: no two field splits collide."""
    data = text.encode()
    return len(data).to_bytes(4, "big") + data


def uuid_v8(*fields: str) -> str:
    """UUIDv8 of sha256 over the length-prefixed fields."""
    digest = bytearray(hashlib.sha256(b"".join(lp(f) for f in fields)).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x80
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))
