#!/usr/bin/env python3
# pyright: strict
"""Keep our A2A subset honest against the vendored proto.

    python3 spec/tools/check_a2a_pin.py   # silent and 0 when clean, else the drift and its fix

Three things drift independently, so all three are checked here:

1. The vendored `spec/schema/a2a/a2a.proto` against `a2a.proto.sha256` and against the sha256 in
   the table in `spec/schema/a2a/README.md`. A protocol bump must move the file, the side file and
   the table together, or the pin means nothing.
2. Every property of `spec/schema/a2a.v1.schema.json` against the proto message of the same name,
   under the protobuf JSON mapping (`context_id` is `contextId` on the wire).
3. Every field the proto marks `REQUIRED` in a message we parse against our `$def`'s `required`,
   and the two enums we accept against the proto's values.

Stdlib only, Python 3.12+. The proto is flat proto3 with no nested messages, so a line-based
parser reads it: a block runs from `message X {` at column 0 to `}` at column 0, and a field is
`[repeated|optional] <type> <name> = <number>[ [options]];` at any depth inside it, which also
picks up the fields of a `oneof` (in JSON a oneof is just the set member's own key).
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fixtures.jcs import JsonValue

A2A = pathlib.Path(__file__).resolve().parents[1] / "schema" / "a2a"
PROTO = A2A / "a2a.proto"
PROTO_SHA = A2A / "a2a.proto.sha256"
README = A2A / "README.md"
SCHEMA = A2A.parent / "a2a.v1.schema.json"

BLOCK = re.compile(r"^(message|enum) (\w+) \{$")
FIELD = re.compile(
    r"^\s*(?:repeated\s+|optional\s+)?(?:map<[^>]+>|[\w.]+)\s+(\w+)\s*=\s*\d+\s*(\[[^\]]*\])?;"
)
ENUM_VALUE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*\d+\s*(?:\[[^\]]*\])?;")
REQUIRED = "(google.api.field_behavior) = REQUIRED"
SHA_ROW = re.compile(r"^\| sha256 \| `([0-9a-f]{64})` \|$", re.MULTILINE)

type Obj = dict[str, JsonValue]

# `$defs` that are not A2A messages: the shared JSON primitives our schema refs.
NOT_MESSAGES = frozenset({"JsonValue", "JsonObject"})

# `$defs` that are messages, but of another protocol the A2A bindings borrow, so the vendored proto
# does not define them. Each needs a reason a reviewer would accept. A name here that is no longer
# in the schema is an error, the same as an unused deviation: the tool must not carry dead excuses.
EXTERNAL_MESSAGES: dict[str, str] = {
    "ErrorInfo": (
        "google.rpc.ErrorInfo, which the HTTP+JSON binding requires in a google.rpc.Status's "
        "`details` and the JSON-RPC binding says an error SHOULD carry; a2a.proto imports no "
        "google/rpc, so the shape is pinned by our schema and this reason rather than by the file"
    ),
}

# A proto field marked REQUIRED that our `$def` leaves optional, keyed by (message, proto field),
# with the reason. Empty: every REQUIRED field of every message we parse is required for us too.
# A new entry needs a reason a reviewer would accept, not a note that the export changed.
TOLERATED: dict[tuple[str, str], str] = {}

# Two deliberate deviations from the proto, both about `SecurityRequirement`, which we never read.
# The pinned specification's own sample card writes AgentCard field 9 as `security`, with a shape
# the proto does not describe, while the proto calls it `security_requirements`. We accept either
# spelling and use neither: `authenticate` is what checks a caller and `bearer` is the only
# credential we send (spec/schema/a2a/README.md records the discrepancy).
EXTRA_PROPERTIES: dict[str, str] = {
    "AgentCard.security": "the sample card's spelling of field 9 `security_requirements`",
}
# The same field on a skill, kept as loose JSON for the same reason.
LOOSE = frozenset({"AgentSkill.securityRequirements"})

# The enums of ours that are the proto's minus its zero value, which is never an answer we can act
# on: a task state that means "unknown or indeterminate", and a role that does not say who sent a
# message. Both are parse errors for us, and the two must stay consistent with each other.
UNSPECIFIED: dict[str, str] = {
    "TaskState": "TASK_STATE_UNSPECIFIED",
    "Role": "ROLE_UNSPECIFIED",
}


@dataclass(frozen=True, slots=True)
class Message:
    """One proto message: its field names, and which of them are REQUIRED."""

    fields: frozenset[str]
    required: frozenset[str]


@dataclass(frozen=True, slots=True)
class Proto:
    messages: dict[str, Message]
    enums: dict[str, tuple[str, ...]]


def _obj(v: JsonValue) -> Obj:
    return v if isinstance(v, dict) else {}


def _strs(v: JsonValue) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def json_name(field: str) -> str:
    """The protobuf JSON mapping: snake_case becomes lowerCamelCase."""
    head, *rest = field.split("_")
    return head + "".join(word[:1].upper() + word[1:] for word in rest)


def parse_proto(text: str) -> Proto:
    """Every message and enum of a flat proto3 file."""
    messages: dict[str, Message] = {}
    enums: dict[str, tuple[str, ...]] = {}
    lines = text.splitlines()
    for start, head in [(i, m) for i, line in enumerate(lines) if (m := BLOCK.match(line))]:
        body = _body(lines, start)
        kind, name = head.group(1), head.group(2)
        if kind == "enum":
            values = (ENUM_VALUE.match(line) for line in body)
            enums[name] = tuple(m.group(1) for m in values if m is not None)
        else:
            messages[name] = _message(body)
    return Proto(messages, enums)


def _body(lines: list[str], start: int) -> list[str]:
    end = next((i for i in range(start + 1, len(lines)) if lines[i] == "}"), len(lines))
    return lines[start + 1 : end]


def _message(body: list[str]) -> Message:
    fields: list[str] = []
    required: list[str] = []
    for line in body:
        found = FIELD.match(line)
        if found is None:
            continue
        fields.append(found.group(1))
        if REQUIRED in (found.group(2) or ""):
            required.append(found.group(1))
    return Message(frozenset(fields), frozenset(required))


def check_pin() -> list[str]:
    """The vendored bytes against both places their hash is written down."""
    digest = hashlib.sha256(PROTO.read_bytes()).hexdigest()
    problems: list[str] = []
    side = PROTO_SHA.read_text(encoding="utf-8").split()
    if side[:1] != [digest]:
        problems.append(
            f"{PROTO_SHA.name} says {side[0] if side else '(nothing)'} but {PROTO.name} hashes to "
            f"{digest}; a protocol bump replaces the file and both places its hash is written"
        )
    row = SHA_ROW.search(README.read_text(encoding="utf-8"))
    if row is None:
        problems.append(f"{README.name}: no `| sha256 | <64 hex> |` row to check the pin against")
    elif row.group(1) != digest:
        problems.append(
            f"{README.name}'s table says {row.group(1)} but {PROTO.name} hashes to {digest}; "
            f"update the table in the same change that replaces the file"
        )
    return problems


def check_enum(name: str, ours: list[str], theirs: tuple[str, ...], *, drop: str) -> list[str]:
    """An enum of ours against the proto's values, with `drop` removed when it is theirs."""
    want = [v for v in theirs if v != drop]
    if ours == want:
        return []
    note = (
        f" ({drop} is the proto's zero value, 'unknown or indeterminate', which is never an "
        f"answer we can act on, so it is a parse error for us)"
        if drop in theirs
        else ""
    )
    return [f"{name}: we accept {ours}, the proto defines {want}{note}"]


def shapes(node: Obj) -> list[Obj]:
    """The object shapes a `$def` allows: itself, or every branch when it is a union.

    A proto `oneof` is untagged in JSON — the set member's own key is the tag — so we model one as a
    union with a branch per member. The message's fields are then spread across the branches, and
    this is what lets the checks below read them all: a field exists for us if any branch has it,
    and counts as required only if every branch requires it."""
    branches = node.get("anyOf") or node.get("oneOf")
    if isinstance(branches, list):
        found = [b for b in branches if isinstance(b, dict)]
        if found:
            return found
    return [node]


def properties_of(node: Obj) -> set[str]:
    """Every property name a `$def` allows, across a union's branches."""
    return {p for shape in shapes(node) for p in _obj(shape.get("properties"))}


def required_of(node: Obj) -> set[str]:
    """The properties a `$def` always requires: required in every branch, not just in one."""
    per_shape = [set(_strs(shape.get("required"))) for shape in shapes(node)]
    return set.intersection(*per_shape) if per_shape else set()


def check_message(name: str, node: Obj, message: Message) -> list[str]:
    """One `$def` against its proto message: our properties exist, their REQUIRED are required."""
    ours = properties_of(node)
    ours_required = required_of(node)
    wire = {json_name(f) for f in message.fields}
    problems = [
        f"{name}.{p}: no field of proto message {name} maps to it"
        for p in sorted(ours - wire)
        if f"{name}.{p}" not in EXTRA_PROPERTIES
    ]
    for field in sorted(message.required):
        prop = json_name(field)
        if prop in ours_required or (name, field) in TOLERATED:
            continue
        problems.append(
            f"{name}.{prop}: the proto marks {field} REQUIRED and we do not read it"
            if prop not in ours
            else f"{name}.{prop}: the proto marks {field} REQUIRED, so it belongs in our "
            f"`required` or in TOLERATED with a reason"
        )
    return problems


def check_schema(schema: Obj, proto: Proto) -> list[str]:
    defs = _obj(schema.get("$defs"))
    if not defs:
        return [f"{SCHEMA.name}: no $defs; run bun run schema:export"]
    problems: list[str] = []
    for name, node in sorted(defs.items()):
        if name in NOT_MESSAGES:
            continue
        if name in EXTERNAL_MESSAGES:
            continue
        if name in proto.enums:
            ours = _strs(_obj(node).get("enum"))
            problems += check_enum(name, ours, proto.enums[name], drop=UNSPECIFIED.get(name, ""))
        elif name in proto.messages:
            problems += check_message(name, _obj(node), proto.messages[name])
        else:
            problems.append(f"{name}: not a message or enum of {PROTO.name}")
    return problems


def check_deviations(schema: Obj) -> list[str]:
    """The two named deviations are still the only ones, and still present: a rename upstream
    would otherwise leave them silently unused."""
    defs = _obj(schema.get("$defs"))
    problems: list[str] = []
    for dotted in sorted(set(EXTRA_PROPERTIES) | LOOSE):
        message, prop = dotted.split(".", 1)
        if prop not in properties_of(_obj(defs.get(message))):
            problems.append(
                f"{dotted}: named in check_a2a_pin.py as a deliberate deviation but not in "
                f"{SCHEMA.name}; drop it from the tool if the Zod schema dropped it"
            )
    for name in sorted(EXTERNAL_MESSAGES):
        if name not in defs:
            problems.append(
                f"{name}: named in check_a2a_pin.py as a message of another protocol but not in "
                f"{SCHEMA.name}; drop it from the tool if the Zod schema dropped it"
            )
    return problems


def main(argv: list[str]) -> int:
    if argv:
        print(__doc__)
        return 2
    schema: Obj = _obj(json.loads(SCHEMA.read_text(encoding="utf-8")))
    proto = parse_proto(PROTO.read_text(encoding="utf-8"))
    problems = check_pin() + check_schema(schema, proto) + check_deviations(schema)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
