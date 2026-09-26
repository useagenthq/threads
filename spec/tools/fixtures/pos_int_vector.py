# pyright: strict
"""`pos-int.json`: the accept/reject table for the one positive-integer check every public entry
point makes on a `PosInt` option (spec/api.json, `urn:threads:schema:events:v1#/$defs/PosInt`),
before anything is written. Both runtimes run their own check over it, which is what proves the
two agree: a fraction, zero, a negative and a boolean are refused in each.

NaN and the infinities are not JSON, so each suite covers them in its own tests. A whole number
written with a fractional part (2.0) is left out on purpose: it is one JSON number, but a Python
float and a JavaScript integer, so it would pin a difference in JSON, not in the boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import CASES
from .pieces import dump

if TYPE_CHECKING:
    from .jcs import JsonValue

VECTOR = CASES.parent / "vectors" / "pos-int.json"

ROWS: tuple[tuple[str, JsonValue, bool], ...] = (
    ("one", 1, True),
    ("the ask and wait default", 120000, True),
    ("a large timeout", 86400000, True),
    ("the largest safe integer", 9007199254740991, True),
    ("one above the safe integer range", 9007199254740992, False),
    ("zero: not positive, so never the default and never immediate", 0, False),
    ("a negative", -1, False),
    ("a fraction", 1.5, False),
    ("a negative fraction", -0.5, False),
    ("true, which is an int subclass in Python", True, False),
    ("false", False, False),
    ("a numeric string", "1", False),
    ("null", None, False),
    ("a list", [1], False),
)


def _rows() -> list[JsonValue]:
    return [{"name": n, "value": v, "pos_int": ok} for n, v, ok in ROWS]


def write() -> None:
    VECTOR.write_text(dump(_rows()))


def check() -> list[str]:
    made = dump(_rows())
    if not VECTOR.is_file() or VECTOR.read_text() != made:
        return [f"{VECTOR.name} is stale; run gen_fixtures.py"]
    return []
