# pyright: strict
"""Semantic rule 20 on the output schemas a Pydantic model writes: annotations (title, default)
never constrain; bounds, lengths, patterns and enums do; nested and recursive models are
followed by $ref. One accepted value satisfies every keyword, and one rejected value breaks each
keyword alone, so both readers agree keyword by keyword."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import sha
from .jcs import canonical
from .log import Log
from .pieces import call, reduce_case, reject, result, started, user
from .policies import FINAL_OUTPUT, OUTPUT, policy

if TYPE_CHECKING:
    import pathlib

    from .jcs import JsonValue, Obj

FAM = "tools_streaming"
# What Pydantic writes for a recursive `Part` (name 2-4 upper-case letters, at most two parts)
# inside an `Estimate` (hours 1-40, a share percent in (0, 100), a size enum, at least a tag).
PART: Obj = {
    "properties": {
        "code": {
            "maxLength": 4,
            "minLength": 2,
            "pattern": "^[A-Z]+$",
            "title": "Code",
            "type": "string",
        },
        "parts": {
            "items": {"$ref": "#/$defs/Part"},
            "maxItems": 2,
            "title": "Parts",
            "type": "array",
        },
    },
    "required": ["code"],
    "title": "Part",
    "type": "object",
}
ESTIMATE: Obj = {
    "$defs": {"Part": PART, "Size": {"enum": ["s", "m"], "title": "Size", "type": "string"}},
    "properties": {
        "hours": {"maximum": 40, "minimum": 1, "title": "Hours", "type": "integer"},
        "share": {
            "exclusiveMaximum": 100,
            "exclusiveMinimum": 0,
            "title": "Share",
            "type": "integer",
        },
        "size": {"$ref": "#/$defs/Size"},
        "tags": {"items": {"type": "string"}, "minItems": 1, "title": "Tags", "type": "array"},
        "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Note"},
        "root": {"$ref": "#/$defs/Part"},
    },
    "required": ["hours", "share", "size", "tags", "root"],
    "title": "Estimate",
    "type": "object",
}
GOOD: Obj = {
    "hours": 8,
    "share": 50,
    "size": "s",
    "tags": ["db"],
    "note": None,
    "root": {"code": "AB", "parts": [{"code": "CD", "parts": [{"code": "EF"}]}]},
}
# (keyword, the field it breaks, a value that breaks only that keyword)
BROKEN: list[tuple[str, str, JsonValue]] = [
    ("minimum", "hours", 0),
    ("maximum", "hours", 41),
    ("exclusive-minimum", "share", 0),
    ("exclusive-maximum", "share", 100),
    ("enum", "size", "l"),
    ("min-items", "tags", []),
    ("min-length", "root", {"code": "A"}),
    ("max-length", "root", {"code": "ABCDE"}),
    ("pattern", "root", {"code": "ab"}),
    (
        "max-items",
        "root",
        {"code": "AB", "parts": [{"code": "CD"}, {"code": "DE"}, {"code": "EF"}]},
    ),
    ("recursive-ref", "root", {"code": "AB", "parts": [{"code": "CD", "parts": [{"code": "e"}]}]}),
]


def build(root: pathlib.Path) -> None:
    log = _accepted(GOOD)
    result(log, "call_1", canonical(GOOD).decode())
    log.add("turn_completed", {"reason": "end_turn"})
    reduce_case(
        root,
        (
            "output-schema-constraints-accepted",
            FAM,
            "The pinned output schema is what a Pydantic model writes: titles and a default "
            "(annotations, never constraints), bounds, exclusive bounds, lengths, a pattern, "
            "enums, and a recursive model by $ref. An accepted value that satisfies every "
            "keyword, at every depth, is valid.",
        ),
        log,
        {},
    )
    for keyword, field, bad in BROKEN:
        reject(
            root,
            (
                f"output-schema-{keyword}-rejected",
                FAM,
                f"output_validated{{accepted}} whose {field} breaks only the {keyword} keyword "
                "of the pinned output schema: invalid_transition. Every keyword is checked, "
                "never skipped.",
            ),
            _accepted({**GOOD, field: bad}),
        )


def _accepted(value: Obj) -> Log:
    """A thread pinned with ESTIMATE and a final_output candidate `value` recorded accepted."""
    output: Obj = {**OUTPUT, "schema": ESTIMATE, "schema_sha256": sha(canonical(ESTIMATE))}
    tool: Obj = {**FINAL_OUTPUT, "input_schema": ESTIMATE}
    log = Log()
    started(log, [tool], policy=policy(output=output))
    user(log, "Estimate the migration.")
    c = call(log, "final_output", value)
    validated: Obj = {
        "source_event_id": c["event_id"],
        "schema_sha256": output["schema_sha256"],
        "outcome": "accepted",
        "value": value,
    }
    log.add("output_validated", validated)
    return log
