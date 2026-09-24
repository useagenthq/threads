"""Test-only JSON Schema check against the spec's own schemas (draft 2020-12, the keywords the
spec uses). Core carries no schema evaluator; tests use this to prove saved cases are valid
against spec/conformance/case.schema.json. An unknown keyword is an error, never skipped."""

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Final

from pydantic import JsonValue, TypeAdapter

SPEC = Path(__file__).resolve().parents[2] / "spec"
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_DOCS: Final[dict[str, JsonValue]] = {}
for _path in (
    SPEC / "schema" / "events.v1.schema.json",
    SPEC / "schema" / "eval.v1.schema.json",
    SPEC / "conformance" / "case.schema.json",
    SPEC / "conformance" / "team-ops.schema.json",
):
    _doc = _JSON.validate_json(_path.read_bytes())
    assert isinstance(_doc, dict)
    _DOCS[str(_doc["$id"])] = _doc
CASE_ID = "urn:threads:schema:conformance:v1"
_ANNOTATIONS = frozenset({"$schema", "$id", "$defs", "description", "title", "default"})


def valid(value: JsonValue, ref: str) -> bool:
    """Whether `value` holds against the schema at `ref` (`<$id>#/<pointer>`)."""
    return _Checker().check(_resolve(ref, CASE_ID)[0], value, _resolve(ref, CASE_ID)[1])


def _resolve(ref: str, base: str) -> tuple[JsonValue, str]:
    doc_id, _, pointer = ref.partition("#")
    doc_id = doc_id or base
    node: JsonValue = _DOCS[doc_id]
    for part in [p for p in pointer.split("/") if p]:
        node = node[int(part)] if isinstance(node, list) else _obj(node)[part]
    return node, doc_id


def _obj(node: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(node, dict), node
    return node


_TYPES: Final[dict[str, Callable[[JsonValue], bool]]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _type(name: JsonValue, v: JsonValue) -> bool:
    return _TYPES[str(name)](v)


def _same(a: JsonValue, b: JsonValue) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


class _Checker:
    def check(self, schema: JsonValue, v: JsonValue, base: str) -> bool:
        if isinstance(schema, bool):
            return schema
        s = _obj(schema)
        rules: dict[str, Callable[[JsonValue], bool]] = {
            "$ref": lambda a: self.check(*_ref(a, base, v)),
            "type": lambda a: _type(a, v),
            "enum": lambda a: isinstance(a, list) and any(_same(x, v) for x in a),
            "const": lambda a: _same(a, v),
            "pattern": lambda a: not isinstance(v, str) or re.search(str(a), v) is not None,
            "minLength": lambda a: not isinstance(v, str) or len(v) >= _int(a),
            "minimum": lambda a: not _type("number", v) or _num(v) >= _num(a),
            "maximum": lambda a: not _type("number", v) or _num(v) <= _num(a),
            "minItems": lambda a: not isinstance(v, list) or len(v) >= _int(a),
            "minProperties": lambda a: not isinstance(v, dict) or len(v) >= _int(a),
            "required": lambda a: not isinstance(v, dict) or all(str(k) in v for k in _list(a)),
            "properties": lambda a: (
                not isinstance(v, dict)
                or all(self.check(sub, v[k], base) for k, sub in _obj(a).items() if k in v)
            ),
            "additionalProperties": lambda a: (
                not isinstance(v, dict)
                or all(
                    self.check(a, x, base)
                    for k, x in v.items()
                    if k not in _obj(s.get("properties", {}))
                )
            ),
            "items": lambda a: not isinstance(v, list) or all(self.check(a, x, base) for x in v),
            "allOf": lambda a: all(self.check(x, v, base) for x in _list(a)),
            "anyOf": lambda a: any(self.check(x, v, base) for x in _list(a)),
            "oneOf": lambda a: sum(self.check(x, v, base) for x in _list(a)) == 1,
            "not": lambda a: not self.check(a, v, base),
            "if": lambda a: self.check(
                s.get("then", True) if self.check(a, v, base) else s.get("else", True), v, base
            ),
            "then": lambda _: True,
            "else": lambda _: True,
        }
        for key, argument in s.items():
            if key in _ANNOTATIONS:
                continue
            if key not in rules:
                raise ValueError(f"unsupported keyword {key}")
            if not rules[key](argument):
                return False
        return True


def _ref(ref: JsonValue, base: str, v: JsonValue) -> tuple[JsonValue, JsonValue, str]:
    node, doc = _resolve(str(ref), base)
    return node, v, doc


def _list(a: JsonValue) -> list[JsonValue]:
    assert isinstance(a, list)
    return a


def _int(a: JsonValue) -> int:
    assert isinstance(a, int)
    return a


def _num(a: JsonValue) -> float:
    assert isinstance(a, int | float)
    return float(a)
