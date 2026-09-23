# pyright: strict
"""Semantic rule 20 on the output schemas a Pydantic model writes: annotations (title, default)
never constrain; bounds, lengths, patterns, enums, formats and multipleOf do; nested and
recursive models are followed by $ref. One accepted value satisfies every keyword, and one
rejected value breaks each keyword alone, so both readers agree keyword by keyword."""

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


# What Pydantic writes for datetime, date, time, UUID, EmailStr and AnyUrl fields (Zod writes the
# same formats, plus a pattern of its own), and a whole-number multipleOf.
MEETING: Obj = {
    "properties": {
        "at": {"format": "date-time", "title": "At", "type": "string"},
        "day": {"format": "date", "title": "Day", "type": "string"},
        "clock": {"format": "time", "title": "Clock", "type": "string"},
        "host": {"format": "email", "title": "Host", "type": "string"},
        "link": {"format": "uri", "minLength": 1, "title": "Link", "type": "string"},
        "id": {"format": "uuid", "title": "Id", "type": "string"},
        "minutes": {"multipleOf": 15, "title": "Minutes", "type": "integer"},
    },
    "required": ["at", "day", "clock", "host", "link", "id", "minutes"],
    "title": "Meeting",
    "type": "object",
}
MEETING_GOOD: Obj = {
    "at": "2024-02-29T23:59:59.25+05:30",
    "day": "2024-02-29",
    "clock": "09:30:00Z",
    "host": "ops.team+standup@mail.example.com",
    "link": "https://example.com/rooms/7?join=1",
    "id": "123E4567-e89b-12d3-a456-426614174000",
    "minutes": 45,
}
MEETING_BROKEN: list[tuple[str, str, JsonValue]] = [
    ("date-time-offset", "at", "2024-02-29T23:59:59"),
    ("date-time-leap-day", "at", "2023-02-29T10:00:00Z"),
    ("date-time-separator", "at", "2024-02-29 10:00:00Z"),
    ("date-month", "day", "2024-13-01"),
    ("time-offset", "clock", "09:30:00"),
    ("time-hour", "clock", "24:00:00Z"),
    ("email", "host", "ops@localhost"),
    ("uri", "link", "example.com/rooms/7"),
    ("uuid", "id", "123e4567e89b12d3a456426614174000"),
    ("multiple-of", "minutes", 50),
]


def build(root: pathlib.Path) -> None:
    _suite(root, ESTIMATE, GOOD, BROKEN, "constraints")
    _suite(root, MEETING, MEETING_GOOD, MEETING_BROKEN, "formats")


def _suite(
    root: pathlib.Path,
    schema: Obj,
    good: Obj,
    broken: list[tuple[str, str, JsonValue]],
    kind: str,
) -> None:
    """One accepted value that satisfies every keyword, then one rejected value per keyword."""
    log = _accepted(schema, good)
    result(log, "call_1", canonical(good).decode())
    log.add("turn_completed", {"reason": "end_turn"})
    reduce_case(
        root,
        (
            f"output-schema-{kind}-accepted",
            FAM,
            f"The pinned output schema is what a Pydantic model writes ({kind}), with titles and "
            "a default (annotations, never constraints). An accepted value that satisfies every "
            "keyword, at every depth, is valid.",
        ),
        log,
        {},
    )
    for keyword, field, bad in broken:
        reject(
            root,
            (
                f"output-schema-{keyword}-rejected",
                FAM,
                f"output_validated{{accepted}} whose {field} breaks only the {keyword} rule of "
                "the pinned output schema (spec/schema/README.md, Output schemas): "
                "invalid_transition. Every keyword is checked, never skipped.",
            ),
            _accepted(schema, {**good, field: bad}),
        )


def _accepted(schema: Obj, value: Obj) -> Log:
    """A thread pinned with `schema` and a final_output candidate `value` recorded accepted."""
    output: Obj = {**OUTPUT, "schema": schema, "schema_sha256": sha(canonical(schema))}
    tool: Obj = {**FINAL_OUTPUT, "input_schema": schema}
    log = Log()
    started(log, [tool], policy=policy(output=output))
    user(log, "Answer with final_output.")
    c = call(log, "final_output", value)
    validated: Obj = {
        "source_event_id": c["event_id"],
        "schema_sha256": output["schema_sha256"],
        "outcome": "accepted",
        "value": value,
    }
    log.add("output_validated", validated)
    return log
