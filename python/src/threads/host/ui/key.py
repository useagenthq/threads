"""A browser's chat key names one thread per (principal, agent, key), by derivation: no lookup
table, and the key itself is never recorded (spec/schema/ui/README.md, "Chat key").
thread_id = UUIDv8 of sha256(lp("threads-ui-v1") || lp(principalKey) || lp(agent) || lp(key))."""

import hashlib
import re
import uuid
from typing import Final

from threads.log import Principal, ThreadId
from threads.log.keys import principal_key

CHAT_KEY: Final = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _lp(text: str) -> bytes:
    """4-byte big-endian UTF-8 length, then the bytes: no two field splits collide."""
    data = text.encode()
    return len(data).to_bytes(4, "big") + data


def ui_thread_id(principal: Principal, agent: str, key: str) -> ThreadId:
    fields = ("threads-ui-v1", principal_key(principal), agent, key)
    digest = bytearray(hashlib.sha256(b"".join(_lp(f) for f in fields)).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x80
    digest[8] = (digest[8] & 0x3F) | 0x80
    return ThreadId(str(uuid.UUID(bytes=bytes(digest))))
