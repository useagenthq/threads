"""SHA-256 in the two hash domains of raw bytes, and RFC 8785 bytes of a value."""

import hashlib

from pydantic import JsonValue

from threads.log.jcs import canonicalize
from threads.result import Err, Ok


def sha256_hex(data: bytes) -> str:
    """Lowercase hex SHA-256 of bytes as they are, never re-serialized (raw domain)."""
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value: JsonValue) -> Ok[str] | Err[str]:
    """SHA-256 of the RFC 8785 bytes of a structured value (canonical domain)."""
    match canonicalize(value):
        case Ok(value=text):
            return Ok(sha256_hex(text.encode("utf-8")))
        case Err() as error:
            return error
