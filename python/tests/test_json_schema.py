"""holds() on what a Pydantic output model writes (semantic rule 20), and unchecked(), which
refuses at setup a schema holds can't fully check."""

import enum
import json
from pathlib import Path
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
        ({"type": "string", "format": "hostname"}, "unsupported format 'hostname'"),
        ({"properties": {"pair": {"prefixItems": [{"type": "integer"}]}}}, "'prefixItems'"),
        ({"items": {"$ref": "https://example.com/x"}}, "unsupported $ref"),
        ({"anyOf": [{"pattern": "("}]}, "doesn't compile"),
    ],
)
def test_unchecked_names_what_holds_cant_check_anywhere(schema: JsonValue, named: str) -> None:
    why = unchecked(schema)
    assert why is not None
    assert named in why


VECTOR = Path(__file__).resolve().parents[2] / "spec/conformance/vectors/multiple-of.json"


def test_multiple_of_is_decimal_exact_on_the_shared_vector() -> None:
    cases = json.loads(VECTOR.read_text())["cases"]
    got = [holds({"multipleOf": c["divisor"]}, c["value"]) for c in cases]
    assert got == [c["multiple"] for c in cases]


@pytest.mark.parametrize(
    ("form", "good", "bad"),
    [
        ("date-time", "2024-02-29T23:59:59.5+05:30", "2023-02-29T10:00:00Z"),
        ("date", "2024-02-29", "2024-13-01"),
        ("time", "10:00:00Z", "10:00:00"),
        ("email", "a.b+c@x-y.example.com", "a@b"),
        ("uri", "urn:isbn:0451450523", "example.com/x"),
        ("uuid", "123E4567-e89b-12d3-a456-426614174000", "123e4567e89b12d3a456426614174000"),
    ],
)
def test_each_format_accepts_and_rejects(form: str, good: str, bad: str) -> None:
    assert holds({"format": form}, good)
    assert not holds({"format": form}, bad)
    assert holds({"format": form}, 7)  # a format constrains strings only
