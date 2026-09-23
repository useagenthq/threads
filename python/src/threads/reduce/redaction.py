"""Where a redaction may land (semantic rule 19): a text part of the result, spans inside its
UTF-8 bytes and on character boundaries. The reducer enforces it; the before_tool_result gate
checks a hook's spans with it before writing one."""

from collections.abc import Iterable

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import Span, TextPart
from threads.reduce.fold import Result


def text_part(result: Result, part: int | MISSING) -> str | None:
    """The text of one part; without content the model sees one text part: the preview."""
    if part is MISSING:
        return None
    if result.content is MISSING:
        return result.preview if part == 0 else None
    if part >= len(result.content):
        return None
    chosen = result.content[part]
    return chosen.text if isinstance(chosen, TextPart) else None


def first_text_part(result: Result) -> int | None:
    """The part a hook's redaction applies to: the first text part."""
    if result.content is MISSING:
        return 0
    return next((i for i, p in enumerate(result.content) if isinstance(p, TextPart)), None)


def span_error(text: str, spans: Iterable[Span]) -> str | None:
    raw = text.encode("utf-8")
    for span in spans:
        if not (0 <= span.start <= span.end <= len(raw)):
            return "a redaction span lies outside its part"
        if not (_on_boundary(raw, span.start) and _on_boundary(raw, span.end)):
            return "a redaction span splits a character"
    return None


def _on_boundary(raw: bytes, offset: int) -> bool:
    # A UTF-8 continuation byte is 0b10xxxxxx; any other byte starts a character.
    return offset == len(raw) or raw[offset] & 0xC0 != 0x80  # noqa: PLR2004
