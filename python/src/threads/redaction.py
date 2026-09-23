"""Secret redaction (spec/schema/README.md "Secret redaction", C5).

Every credential value the host resolves is registered here; nothing is recorded with one in
it. Event data is redacted where the writer stores it (`store/lines.py`), so every event
(results, model output, hook text, input) passes one boundary; the bytes stored beside events
(spilled output, commits) are redacted where they are written, streams included.
"""

import re
from collections.abc import Callable

from pydantic import JsonValue

_REGISTERED: dict[str, str] = {}
"""Resolved values, each with the smallest label it was registered under."""


def register(value: str, label: str) -> None:
    """Registers a resolved value; recorded text shows `[secret <label>]` instead."""
    known = _REGISTERED.get(value)
    if known is None or label < known:
        _REGISTERED[value] = label


def _marker(label: str) -> str:
    """The marker for a value: its label, unless some registered value appears in it (`api` in
    `[secret fake.api_key]`); then the first plainer marker that holds none. A marker never
    brings a value back."""
    for marker in (f"[secret {label}]", "[secret]", "[redacted]"):
        if not any(v in marker for v in _REGISTERED):
            return marker
    return ""


def _replacements(encode: Callable[[str], str]) -> dict[str, str]:
    """Value to replacement, longest value first, then by code point."""
    ordered = sorted(_REGISTERED.items(), key=lambda item: (-len(item[0]), item[0]))
    return {encode(v): encode(_marker(label)) for v, label in ordered}


def _held_from(pending: str, values: dict[str, str]) -> int:
    """The first position whose rest is a proper prefix of a value: it may still become one."""
    longest = max(len(v) for v in values)
    for i in range(max(0, len(pending) - longest + 1), len(pending)):
        tail = pending[i:]
        if any(len(v) > len(tail) and v.startswith(tail) for v in values):
            return i
    return len(pending)


def _scan(pending: str, values: dict[str, str], *, final: bool) -> tuple[str, str]:
    """The longest value at each position, scanning left to right. Unless `final`, it stops
    where the rest could still grow into a longer value (`abc` before a possible `abc123`), and
    returns that rest to hold for the next chunk."""
    if not values:
        return pending, ""
    hold = len(pending) if final else _held_from(pending, values)
    pattern = re.compile("|".join(re.escape(v) for v in values))
    out: list[str] = []
    at = 0
    for m in pattern.finditer(pending):
        if m.start() >= hold:
            break
        out += [pending[at : m.start()], values[m.group(0)]]
        at = m.end()
    cut = max(hold, at)
    out.append(pending[at:cut])
    return "".join(out), pending[cut:]


def _same(text: str) -> str:
    return text


def redact_secrets(text: str) -> str:
    """`text` with every resolved value replaced by its label."""
    return _scan(text, _replacements(_same), final=True)[0]


def redact_json(value: JsonValue) -> JsonValue:
    """`value` with every string in it redacted, object keys included: structured data about
    to be recorded. A redacted key that meets another is numbered, so no entry is lost."""
    match value:
        case str():
            return redact_secrets(value)
        case list():
            return [redact_json(v) for v in value]
        case dict():
            return _redact_object(value)
        case _:
            return value


def _redact_object(value: dict[str, JsonValue]) -> JsonValue:
    out: dict[str, JsonValue] = {}
    for key, item in value.items():
        base = name = redact_secrets(key)
        n = 2
        while name in out:
            name, n = f"{base} ({n})", n + 1
        out[name] = redact_json(item)
    return out


def redact_bytes(data: bytes) -> bytes:
    """`data` with every resolved value replaced (a textual artifact about to be stored)."""
    stream = StreamRedactor()
    return stream.feed(data) + stream.end()


def holds_secret(data: bytes) -> bool:
    """Whether `data` holds a resolved value."""
    return redact_bytes(data) != data


class SecretInProviderOutputError(Exception):
    """Provider continuation material (signed or encrypted reasoning, a hosted tool's item) is
    replayed byte-exact, so it is never edited: one that holds a resolved value is refused."""

    def __init__(self) -> None:
        super().__init__("a registered secret appeared in unmodifiable provider output")


def _as_bytes(text: str) -> str:
    """A value as the latin-1 text of its UTF-8 bytes: bytes are scanned one char per byte, so
    a value split inside a multi-byte character is held and matched like any other."""
    return text.encode("utf-8").decode("latin-1")


class StreamRedactor:
    """Bytes redacted as they stream into a recorded artifact (a spilled exec output). The
    unredacted tail that could still grow into a longer value is held until it is decided, so a
    value split across chunks, even inside a multi-byte character, is replaced whole."""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, chunk: bytes) -> bytes:
        self._pending += chunk.decode("latin-1")
        return self._take(final=False)

    def end(self) -> bytes:
        return self._take(final=True)

    def _take(self, *, final: bool) -> bytes:
        out, self._pending = _scan(self._pending, _replacements(_as_bytes), final=final)
        return out.encode("latin-1")
