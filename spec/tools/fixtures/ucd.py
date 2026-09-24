# pyright: strict
"""The Unicode 15.0.0 tables tool_search matches with (spec/schema/README.md, "tool_search"),
read from the committed UCD files in spec/unicode/15.0.0, and the reference fold, query split and
tokens. spec/tools/gen_unicode_fold.py embeds the same tables in both implementations."""

from __future__ import annotations

import functools
import pathlib
import unicodedata
from dataclasses import dataclass

UCD = pathlib.Path(__file__).resolve().parents[2] / "unicode" / "15.0.0"
VERSION = "15.0.0"
EXTRA_SEPARATORS = (0x2C, 0x1C, 0x1D, 0x1E, 0x1F, 0xFEFF)
"""`,`, and the code points one runtime treats as whitespace and the other doesn't."""


@dataclass(frozen=True, slots=True)
class Tables:
    assigned: tuple[tuple[int, int], ...]
    """Inclusive ranges of the code points assigned in 15.0.0, surrogates excluded."""
    word: tuple[tuple[int, int], ...]
    """Inclusive ranges of General_Category L*, M* and N*."""
    separators: frozenset[int]
    fold: dict[int, tuple[int, ...]]
    """Full case folding: status C and F."""


def _fields(name: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in (UCD / name).read_text(encoding="utf-8").splitlines():
        data = line.split("#", 1)[0].strip()
        if data:
            rows.append([f.strip() for f in data.split(";")])
    return rows


def _span(field: str) -> tuple[int, int]:
    lo, _, hi = field.partition("..")
    return int(lo, 16), int(hi or lo, 16)


def _merged(spans: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    out: list[tuple[int, int]] = []
    for lo, hi in sorted(spans):
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return tuple(out)


@functools.cache
def tables() -> Tables:
    categories = [(_span(r[0]), r[1]) for r in _fields("DerivedGeneralCategory.txt")]
    assigned = _merged([s for s, gc in categories if gc not in ("Cn", "Cs")])
    word = _merged([s for s, gc in categories if gc[0] in "LMN"])
    space: set[int] = set(EXTRA_SEPARATORS)
    for r in _fields("PropList.txt"):
        if r[1] == "White_Space":
            lo, hi = _span(r[0])
            space.update(range(lo, hi + 1))
    fold = {
        int(r[0], 16): tuple(int(x, 16) for x in r[2].split())
        for r in _fields("CaseFolding.txt")
        if r[1] in ("C", "F")
    }
    return Tables(assigned, word, frozenset(space), fold)


def _within(ranges: tuple[tuple[int, int], ...], cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in ranges)


def _assigned_only(s: str) -> str:
    """Step 1: a code point not assigned in 15.0.0 (or a surrogate) becomes U+0020."""
    t = tables()
    return "".join(c if _within(t.assigned, ord(c)) else " " for c in s)


def fold(s: str) -> str:
    t = tables()
    once = unicodedata.normalize("NFKC", _assigned_only(s))
    folded = "".join("".join(map(chr, t.fold.get(ord(c), (ord(c),)))) for c in once)
    return unicodedata.normalize("NFKC", folded)


def terms(query: str) -> list[str]:
    """The query split for exact names: step 1 and NFKC, then separators, empty terms dropped."""
    seps = tables().separators
    out: list[str] = []
    cur: list[str] = []
    for c in unicodedata.normalize("NFKC", _assigned_only(query)):
        if ord(c) in seps:
            if cur:
                out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    if cur:
        out.append("".join(cur))
    return out


def tokens(s: str) -> list[str]:
    """Maximal runs of L, M and N code points of fold(s), in order (duplicates kept)."""
    word = tables().word
    out: list[str] = []
    cur: list[str] = []
    for c in fold(s):
        if _within(word, ord(c)):
            cur.append(c)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out
