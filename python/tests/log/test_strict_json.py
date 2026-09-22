"""parse_json admission rules, including a round-trip property."""

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue

from threads.log.strict_json import MAX_SAFE_INTEGER, parse_json
from threads.result import Err, Ok

SAFE_INTS = st.integers(min_value=-MAX_SAFE_INTEGER, max_value=MAX_SAFE_INTEGER)
FLOATS = st.floats(allow_nan=False, allow_infinity=False).filter(
    lambda f: not (f.is_integer() and abs(f) > MAX_SAFE_INTEGER)
)
TEXT = st.text(st.characters(exclude_categories=("Cs",)))
JSON_VALUES: st.SearchStrategy[JsonValue] = st.recursive(
    st.none() | st.booleans() | SAFE_INTS | FLOATS | TEXT,
    lambda children: st.lists(children) | st.dictionaries(TEXT, children),
    max_leaves=20,
)


@given(JSON_VALUES)
def test_admissible_values_round_trip(value: JsonValue) -> None:
    assert parse_json(json.dumps(value)) == Ok(value)


@given(TEXT, JSON_VALUES, JSON_VALUES)
def test_duplicate_keys_are_rejected(key: str, first: JsonValue, second: JsonValue) -> None:
    text = f"{{{json.dumps(key)}:{json.dumps(first)},{json.dumps(key)}:{json.dumps(second)}}}"
    assert isinstance(parse_json(text), Err)


@pytest.mark.parametrize(
    ("text", "admitted"),
    [
        ('"\\ud83d\\ude00"', True),
        ('"\\ud83d"', False),
        ('"\\ude00\\ud83d"', False),
        ("9007199254740991", True),
        ("-9007199254740991", True),
        ("9007199254740992", False),
        ("-9007199254740992", False),
        ("9007199254740991.0", True),
        ("1e16", False),
        ("1.5e300", False),
        ("5e-324", True),
        ("1.7976931348623157e+308", False),
        ("1e309", False),
        ("NaN", False),
        ("-Infinity", False),
        ("[" * 100_000 + "]" * 100_000, False),
    ],
)
def test_admission_boundaries(text: str, *, admitted: bool) -> None:
    assert isinstance(parse_json(text), Ok) is admitted
