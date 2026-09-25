# pyright: strict
"""The team wire vector (spec/conformance/vectors/team-wire.json): one event line per case, and
whether the line schema admits it. Both runtimes parse each line with their line parser (Zod in
TypeScript, the generated Pydantic models in Python) and must agree with `valid`. The decisions
are authored from the schema rules, never read from an implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import CASES
from .pieces import dump
from .team_wire_events import EVENT_CASES
from .team_wire_host import HOST_CASES
from .team_wire_mail import MAIL_CASES

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

VECTOR = CASES.parent / "vectors" / "team-wire.json"


def _vector() -> str:
    cases: list[JsonValue] = [
        {"name": n, "line": ln, "valid": v} for n, ln, v in (*EVENT_CASES, *MAIL_CASES, *HOST_CASES)
    ]
    doc: Obj = {
        "description": (
            "Team event lines (spec/schema/README.md, Teams): each line is parsed with the line "
            "schema (Zod in TypeScript, the generated Pydantic models in Python) and must be "
            "admitted exactly when valid; a refused line is invalid_line."
        ),
        "cases": cases,
    }
    return dump(doc)


def write() -> None:
    VECTOR.write_text(_vector(), encoding="utf-8")


def check() -> list[str]:
    fresh = _vector()
    current = VECTOR.read_text(encoding="utf-8") if VECTOR.exists() else ""
    return [] if fresh == current else [f"{VECTOR.name}: differs; run gen_fixtures.py"]
