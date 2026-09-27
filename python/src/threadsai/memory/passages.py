"""A document's passages with their UTF-8 byte spans (the wire `Span`, never code units)."""

import re
from collections.abc import Iterator
from typing import Final

_SPACES: Final = (0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F, 0x3000)
WHITESPACE: Final = "\t\n\v\f\r " + "".join(map(chr, (*_SPACES, 0xFEFF)))
"""ECMAScript's WhiteSpace and LineTerminator (what TS `\\s` and `trim` use), not `str.isspace`:
both languages split and trim a shared store's documents at the same bytes."""

_BREAK = re.compile(f"\n[{WHITESPACE}]*\n")


def split(text: str) -> Iterator[tuple[tuple[int, int], str]]:
    """Paragraphs separated by blank lines, each with its [start, end) byte span in `text`."""
    start = 0
    for found in (*_BREAK.finditer(text), None):
        end = len(text) if found is None else found.start()
        body = text[start:end]
        if body.strip(WHITESPACE):
            lead = len(body) - len(body.lstrip(WHITESPACE))
            body = body.strip(WHITESPACE)
            first = len(text[: start + lead].encode("utf-8"))
            yield (first, first + len(body.encode("utf-8"))), body
        if found is not None:
            start = found.end()
