"""A document's passages with their UTF-8 byte spans (the wire `Span`, never code units)."""

import re
from collections.abc import Iterator

_BREAK = re.compile(r"\n\s*\n")


def split(text: str) -> Iterator[tuple[tuple[int, int], str]]:
    """Paragraphs separated by blank lines, each with its [start, end) byte span in `text`."""
    start = 0
    for found in (*_BREAK.finditer(text), None):
        end = len(text) if found is None else found.start()
        body = text[start:end]
        if body.strip():
            lead = len(body) - len(body.lstrip())
            body = body.strip()
            first = len(text[: start + lead].encode("utf-8"))
            yield (first, first + len(body.encode("utf-8"))), body
        if found is not None:
            start = found.end()
