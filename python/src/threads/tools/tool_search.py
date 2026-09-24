"""tool_search matching (spec/schema/README.md, "Deferred tools and tool_search"): the normative
fold, the exact-name split and keyword tokens, all from the generated Unicode 15.0.0 table, so
both runtimes answer byte for byte whatever Unicode version they ship.

Nothing here lowercases, strips or splits with the runtime's own notion of case or whitespace:
tests/tools/test_tool_search_lint.py greps for it."""

import unicodedata
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from threads._generated.fold_table import ASSIGNED, FOLD, SEPARATORS, WORD

NO_MATCH: Final = "no deferred tool matches"
MAX_EXACT: Final = 10
MAX_QUERY: Final = 200
"""Code points: a JSON Schema maxLength counts differently in the two validators."""
_ASSIGNED_STARTS: Final = tuple(lo for lo, _ in ASSIGNED)
_WORD_STARTS: Final = tuple(lo for lo, _ in WORD)


@dataclass(frozen=True, slots=True)
class Found:
    lines: tuple[str, ...]
    """The result text's lines, in result order."""
    loaded: tuple[str, ...]
    """The deferred tools this search loads, in result order: what tools_loaded names."""


def _within(ranges: Sequence[tuple[int, int]], starts: Sequence[int], cp: int) -> bool:
    at = bisect_right(starts, cp) - 1
    return at >= 0 and cp <= ranges[at][1]


def _assigned_only(s: str) -> str:
    """Step 1: a code point not assigned in Unicode 15.0.0, or a surrogate, becomes U+0020."""
    return "".join(c if _within(ASSIGNED, _ASSIGNED_STARTS, ord(c)) else " " for c in s)


def fold(s: str) -> str:
    once = unicodedata.normalize("NFKC", _assigned_only(s))
    return unicodedata.normalize("NFKC", "".join(FOLD.get(ord(c), c) for c in once))


def terms(query: str) -> list[str]:
    """The exact-name split: step 1 and NFKC, then the pinned separators, empty terms dropped."""
    out: list[str] = []
    current: list[str] = []
    for c in unicodedata.normalize("NFKC", _assigned_only(query)):
        if ord(c) in SEPARATORS:
            if current:
                out.append("".join(current))
            current = []
        else:
            current.append(c)
    if current:
        out.append("".join(current))
    return out


def tokens(s: str) -> list[str]:
    """Maximal runs of General_Category L, M or N code points of fold(s), duplicates kept."""
    out: list[str] = []
    current: list[str] = []
    for c in fold(s):
        if _within(WORD, _WORD_STARTS, ord(c)):
            current.append(c)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


def search(
    query: str, limit: int, deferred: Sequence[tuple[str, str]], others: Sequence[str]
) -> Found:
    """`deferred`: (name, description) of each still-deferred tool, in tool-set order;
    `others`: every other current tool's name. Exact names first, then keywords, else no match."""
    exact = _exact(query, deferred, others)
    if exact is not None:
        return exact
    ranked = _ranked(query, limit, deferred)
    if not ranked:
        return Found((NO_MATCH,), ())
    return Found(tuple(f"{n}: {_first_line(d)}" for n, d in ranked), tuple(n for n, _ in ranked))


def _first_line(description: str) -> str:
    return description.split("\n", 1)[0]


def _exact(query: str, deferred: Sequence[tuple[str, str]], others: Sequence[str]) -> Found | None:
    names = {fold(n): n for n, _ in deferred} | {fold(n): n for n in others}
    found = [fold(t) for t in terms(query)]
    if not found or any(t not in names for t in found):
        return None
    described = dict(deferred)
    lines: list[str] = []
    load: list[str] = []
    seen: set[str] = set()
    for name in (names[t] for t in found):
        if name in seen:
            continue
        seen.add(name)
        if name not in described:
            lines.append(f"{name}: already loaded")
        elif len(load) < MAX_EXACT:
            load.append(name)
            lines.append(f"{name}: {_first_line(described[name])}")
    return Found(tuple(lines), tuple(load))


def _ranked(query: str, limit: int, deferred: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    wanted = set(tokens(query))
    scored = [(len(wanted & set(tokens(f"{n} {d}"))), n, d) for n, d in deferred]
    # Python orders str by code point, as the spec requires.
    kept = sorted((-score, n, d) for score, n, d in scored if score > 0)
    return [(n, d) for _, n, d in kept[:limit]]
