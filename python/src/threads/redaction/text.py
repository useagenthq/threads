"""Text redaction: every string the writer records and every text artifact the host stores."""

from pydantic import JsonValue

from threads.redaction.registry import holds
from threads.redaction.scan import Stream, replacements, scan


def _same(text: str) -> str:
    return text


def text_stream() -> Stream:
    """Streamed text (model deltas) redacted as it arrives, a value split across chunks
    included: `feed` each chunk, then `end` for the held tail."""
    return Stream(_same)


def redact_secrets(text: str) -> str:
    """`text` with every registered value replaced by its marker. If the result still holds a
    value (a marker joined to its neighbours), the whole string becomes `[redacted]`."""
    r = replacements(_same)
    out, _ = scan(text, r, final=True)
    return r.redacted if r.holds(out) else out


def redact_json(value: JsonValue) -> JsonValue:
    """`value` with every string in it redacted, object keys included: structured data about
    to be recorded. A redacted key that meets another is numbered; a number that would hold a
    value is skipped, so no entry is lost and no key holds a value."""
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
        while name in out or holds(name):
            name, n = f"{base} ({n})", n + 1
        out[name] = redact_json(item)
    return out
