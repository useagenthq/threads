"""holds(): the JSON Schema keyword subset that generated models check at runtime."""

import pytest
from pydantic import JsonValue

from threads._strict_model import holds

RULE_IF: JsonValue = {
    "if": {"properties": {"k": {"const": "a"}}},
    "then": {"required": ["x"]},
    "else": {"not": {"required": ["x"]}},
}


@pytest.mark.parametrize(
    ("schema", "value", "expected"),
    [
        (RULE_IF, {"k": "a", "x": 1}, True),
        (RULE_IF, {"k": "a"}, False),
        (RULE_IF, {"k": "b"}, True),
        (RULE_IF, {"k": "b", "x": 1}, False),
        ({"oneOf": [{"required": ["a"]}, {"required": ["b"]}]}, {"a": 1}, True),
        ({"oneOf": [{"required": ["a"]}, {"required": ["b"]}]}, {"a": 1, "b": 2}, False),
        ({"oneOf": [{"required": ["a"]}, {"required": ["b"]}]}, {}, False),
        ({"anyOf": [{"required": ["a"]}, {"required": ["b"]}]}, {"b": 1}, True),
        ({"anyOf": [{"required": ["a"]}, {"required": ["b"]}]}, {}, False),
        ({"allOf": [{"required": ["a"]}, {"required": ["b"]}]}, {"a": 1}, False),
        ({"minProperties": 1}, {}, False),
        ({"minProperties": 1}, {"a": None}, True),
        ({"properties": {"a": {"enum": ["x", "y"]}}}, {"a": "y"}, True),
        ({"properties": {"a": {"enum": ["x", "y"]}}}, {"a": "z"}, False),
        ({"properties": {"a": {"const": True}}}, {"a": 1}, False),
        ({"properties": {"a": {"const": 1}}}, {"a": True}, False),
        ({"properties": {"a": {"const": "x"}}}, {}, True),
        ({"required": ["a"]}, "not an object", True),
    ],
)
def test_holds(schema: JsonValue, value: object, *, expected: bool) -> None:
    assert holds(schema, value) is expected


def test_unsupported_keyword_is_a_bug() -> None:
    with pytest.raises(TypeError, match="unsupported schema keyword"):
        holds({"pattern": "^a$"}, "a")
