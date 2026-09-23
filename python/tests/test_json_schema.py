"""holds() on what a Pydantic output model writes (semantic rule 20), and unchecked(), which
refuses at setup a schema holds can't fully check."""

import enum
from typing import Literal

import pytest
from pydantic import BaseModel, Field, JsonValue

from threads._json_schema import holds, unchecked


class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class Node(BaseModel):
    name: str = Field(min_length=1, max_length=5, pattern=r"^[a-z]+$")
    children: list["Node"] = Field(default_factory=list["Node"], max_length=2)


class Order(BaseModel):
    qty: int = Field(ge=1, le=5)
    share: float = Field(gt=0, lt=1)
    kind: Literal["x", "y"]
    color: Color
    tags: list[str] = Field(min_length=1)
    tree: Node


ORDER: JsonValue = Order.model_json_schema()
GOOD: dict[str, JsonValue] = {
    "qty": 3,
    "share": 0.5,
    "kind": "x",
    "color": "red",
    "tags": ["a"],
    "tree": {"name": "root", "children": [{"name": "leaf", "children": []}]},
}


def test_a_value_the_model_accepts_holds() -> None:
    assert unchecked(ORDER) is None
    assert holds(ORDER, GOOD)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("qty", 0),  # minimum (ge)
        ("qty", 6),  # maximum (le)
        ("share", 0),  # exclusiveMinimum (gt)
        ("share", 1),  # exclusiveMaximum (lt)
        ("kind", "z"),  # enum from Literal
        ("color", "green"),  # enum from an Enum, by $ref
        ("tags", []),  # minItems
        ("tree", {"name": ""}),  # minLength
        ("tree", {"name": "toolong"}),  # maxLength
        ("tree", {"name": "Root"}),  # pattern
        ("tree", {"name": "a", "children": [{"name": "b"}] * 3}),  # maxItems
        ("tree", {"name": "a", "children": [{"name": "B1"}]}),  # recursive $ref
    ],
)
def test_each_constraint_is_checked(field: str, bad: JsonValue) -> None:
    assert not holds(ORDER, {**GOOD, field: bad})


def test_a_recursive_model_is_checked_at_any_depth() -> None:
    schema = Node.model_json_schema()
    deep: JsonValue = {"name": "a", "children": [{"name": "b", "children": [{"name": "C"}]}]}
    assert unchecked(schema) is None
    assert not holds(schema, deep)


@pytest.mark.parametrize(
    ("schema", "named"),
    [
        ({"type": "string", "format": "date-time"}, "'format'"),
        ({"properties": {"n": {"multipleOf": 2}}}, "'multipleOf'"),
        ({"items": {"$ref": "https://example.com/x"}}, "unsupported $ref"),
        ({"anyOf": [{"pattern": "("}]}, "doesn't compile"),
    ],
)
def test_unchecked_names_what_holds_cant_check_anywhere(schema: JsonValue, named: str) -> None:
    why = unchecked(schema)
    assert why is not None
    assert named in why
