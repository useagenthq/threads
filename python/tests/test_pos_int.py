"""The shared PosInt vector (spec/conformance/vectors/pos-int.json): the one check every public
entry point makes on a positive-integer option, and the same table TypeScript runs. NaN and the
infinities are not JSON, so they are asserted here."""

import math
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from threads.pos_int import is_pos_int

VECTORS = Path(__file__).resolve().parents[2] / "spec" / "conformance" / "vectors"
_LIST: TypeAdapter[list[dict[str, JsonValue]]] = TypeAdapter(list[dict[str, JsonValue]])
_ENTRIES = _LIST.validate_json((VECTORS / "pos-int.json").read_bytes())


@pytest.mark.parametrize("entry", _ENTRIES, ids=lambda e: str(e["name"]))
def test_the_shared_table(entry: dict[str, JsonValue]) -> None:
    assert is_pos_int(entry["value"]) is entry["pos_int"]


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_a_non_finite_float_is_not_a_positive_integer(value: float) -> None:
    assert is_pos_int(value) is False
