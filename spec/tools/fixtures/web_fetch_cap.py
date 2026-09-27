# pyright: strict
"""`web-fetch-cap.json`: where `web_fetch` cuts a response, and what the kept bytes hash to.

The cap itself is `spec/schema/limits.json`'s `web_fetch.max_bytes`, generated into both languages.
This vector pins the boundary: each suite serves a body of `served` bytes through its own transport
and must keep `kept` of them, hashing to `sha256`. A cap that moved in one language alone would
change the content hash a run records for the same URL, and would fail here first.

Byte i of a served body is `i % 251`, so a body is the prefix of every longer one and keeping the
wrong end of the page hashes differently rather than the same."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from .common import CASES
from .pieces import dump

if TYPE_CHECKING:
    from .jcs import JsonValue

VECTOR = CASES.parent / "vectors" / "web-fetch-cap.json"
LIMITS = CASES.parents[1] / "schema" / "limits.json"

DESCRIPTION = (
    "Where web_fetch cuts a response. Byte i of the served body is i % 251; a fetch of a body of"
    " `served` bytes keeps `kept` of them (its first ones), and their sha256 is `sha256`. max_bytes"
    " is spec/schema/limits.json's web_fetch.max_bytes, which both languages generate in: each"
    " suite checks its own constant against it, then checks the boundary."
)


def _cap() -> int:
    source = json.loads(LIMITS.read_text(encoding="utf-8"))
    cap = source["web_fetch"]["max_bytes"]
    if not isinstance(cap, int):
        raise AssertionError("web_fetch.max_bytes is an integer")
    return cap


def _body(n: int) -> bytes:
    """Byte i is i % 251, built by repeating the one cycle rather than a byte at a time."""
    cycle = bytes(range(251))
    return (cycle * (n // len(cycle) + 1))[:n]


def _vector() -> JsonValue:
    cap = _cap()
    # Every body is a prefix of the longest one, so the bytes are built once.
    full = _body(cap + 1)
    rows: list[JsonValue] = []
    for name, served in (
        ("a page well under the cap", 1024),
        ("a page one byte short of the cap", cap - 1),
        ("a page of exactly the cap", cap),
        ("a page one byte past the cap: cut, and the last byte is not kept", cap + 1),
    ):
        kept = min(served, cap)
        rows.append(
            {
                "name": name,
                "served": served,
                "kept": kept,
                "sha256": hashlib.sha256(full[:kept]).hexdigest(),
            }
        )
    return {"description": DESCRIPTION, "max_bytes": cap, "cases": rows}


def write() -> None:
    VECTOR.write_text(dump(_vector()))


def check() -> list[str]:
    made = dump(_vector())
    if not VECTOR.is_file() or VECTOR.read_text() != made:
        return [f"{VECTOR.name} is stale; run gen_fixtures.py"]
    return []
