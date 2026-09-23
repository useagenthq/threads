"""The test-only schema check agrees with the corpus and rejects what the schema forbids."""

import json
from typing import TYPE_CHECKING

from corpus import CASES
from schema_check import CASE_ID, valid

if TYPE_CHECKING:
    from pydantic import JsonValue

CASE = f"{CASE_ID}#/$defs/Case"
EXPECTED = f"{CASE_ID}#/$defs/Expected"


def test_every_corpus_case_is_valid() -> None:
    for case in CASES.iterdir():
        assert valid(json.loads((case / "case.json").read_bytes()), CASE), case.name
        for expected in case.glob("expected*.json"):
            assert valid(json.loads(expected.read_bytes()), EXPECTED), expected


def test_invalid_cases_are_refused() -> None:
    good: dict[str, JsonValue] = json.loads(
        (CASES / "stub-occurrence-order" / "case.json").read_bytes()
    )
    bads: list[JsonValue] = [
        {**good, "kind": "nope"},
        {**good, "surprise": 1},
        {k: v for k, v in good.items() if k != "clock"},
        {**good, "expect": {"must": []}},
        {**good, "expect": {"must": [{"type": "fork", "seq": 0}]}},
    ]
    assert valid(good, CASE)
    for bad in bads:
        assert not valid(bad, CASE), bad
