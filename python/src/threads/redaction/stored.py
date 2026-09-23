"""Bytes the host stores: redacted when they are text it may edit (a spilled exec output, a
fetched page), refused when they must stay byte-exact (provider material, a screenshot, a
knowledge source, the resolved config, an import)."""

import json
from collections.abc import Callable, Sequence

from threads.redaction.registry import PAUSED, generation, ordered
from threads.redaction.scan import Stream, replacements, scan


def _as_bytes(text: str) -> str:
    """A value as the latin-1 text of its UTF-8 bytes: bytes are scanned one char per byte, so
    a value split inside a multi-byte character is held and matched like any other."""
    return text.encode("utf-8").decode("latin-1")


def _forms(value: str) -> set[bytes]:
    """A value's forms in stored bytes: as is, JSON-escaped, and JSON-escaped to ASCII."""
    escaped = json.dumps(value, ensure_ascii=False)[1:-1]
    ascii_only = json.dumps(value)[1:-1]
    return {f.encode("utf-8") for f in (value, escaped, ascii_only)}


def contains_secret(data: bytes) -> bool:
    """Whether `data` holds a registered value in any form a store or a later parse could give
    back: raw UTF-8, or JSON-escaped (quotes, backslashes, controls, `\\uXXXX`)."""
    return any(form in data for value, _ in ordered() for form in _forms(value))


def redact_bytes(data: bytes) -> bytes:
    """`data` (a textual artifact) with every registered value replaced. Bytes that still hold
    one, or its JSON-escaped form, are the caller's to refuse (`contains_secret`)."""
    r = replacements(_as_bytes)
    out, _ = scan(data.decode("latin-1"), r, final=True)
    return (r.redacted if r.holds(out) else out).encode("latin-1")


class SecretInStoredBytesError(Exception):
    """Bytes that held a registered value when the store's thread came to publish them: a value
    was registered after the caller's own check. Nothing was written."""

    def __init__(self) -> None:
        super().__init__("a registered secret appeared in bytes about to be stored")


def published[T](data: bytes | Sequence[bytes], publish: Callable[[], T]) -> T:
    """Runs `publish` (on the store's thread) with registration paused, unless `data` (one
    piece of bytes, or several checked one by one) holds a registered value by then:
    SecretInStoredBytesError, nothing written."""
    pieces = (data,) if isinstance(data, bytes) else data
    with PAUSED:
        if any(contains_secret(piece) for piece in pieces):
            raise SecretInStoredBytesError
        return publish()


def unchanged_since[T](seen: int, publish: Callable[[], T]) -> T | None:
    """Runs `publish` with registration paused, unless a value was registered after the
    generation `seen` (None): bytes already streamed out can't be checked again."""
    with PAUSED:
        return publish() if generation() == seen else None


class SecretInProviderOutputError(Exception):
    """Provider continuation material (signed or encrypted reasoning, a hosted tool's item) is
    replayed byte-exact, so it is never edited: one that holds a registered value is refused."""

    def __init__(self) -> None:
        super().__init__("a registered secret appeared in unmodifiable provider output")


class StreamRedactor:
    """Bytes redacted as they stream into a recorded artifact (a spilled exec output). The
    unredacted tail that could still grow into a longer value is held until it is decided, so a
    value split across chunks, even inside a multi-byte character, is replaced whole."""

    def __init__(self) -> None:
        self._stream = Stream(_as_bytes)

    def feed(self, chunk: bytes) -> bytes:
        return self._stream.feed(chunk.decode("latin-1")).encode("latin-1")

    def end(self) -> bytes:
        return self._stream.end().encode("latin-1")
