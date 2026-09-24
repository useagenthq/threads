"""The team op vector before its build (spec/conformance/vectors/team-ops.json): the file holds
to its schema, and every event of every world passes the line schema. The Teams build runs the
vectors themselves (lanes 21C-21F)."""

import json
from pathlib import Path

import pytest
from pydantic import JsonValue
from schema_check import valid

from threads.log import parse_log_line
from threads.log.jcs import canonicalize
from threads.result import Ok

SPEC = Path(__file__).resolve().parents[3] / "spec"
DOC: JsonValue = json.loads(
    (SPEC / "conformance" / "vectors" / "team-ops.json").read_text(encoding="utf-8")
)


def _events() -> list[tuple[str, dict[str, JsonValue]]]:
    assert isinstance(DOC, dict)
    events = DOC["events"]
    assert isinstance(events, dict)
    out: list[tuple[str, dict[str, JsonValue]]] = []
    for ident, event in events.items():
        assert isinstance(event, dict)
        out.append((ident, event))
    return out


def test_the_file_holds_to_its_schema() -> None:
    assert valid(DOC, "urn:threads:schema:team-ops:v1#")


def test_an_invalid_file_is_refused() -> None:
    assert isinstance(DOC, dict)
    assert not valid({**DOC, "constants": {}}, "urn:threads:schema:team-ops:v1#")


@pytest.mark.parametrize(("ident", "event"), _events(), ids=[i for i, _ in _events()])
def test_every_world_event_is_a_valid_line(ident: str, event: dict[str, JsonValue]) -> None:
    line = canonicalize({**event, "prev_hash": "0" * 64})
    assert isinstance(line, Ok), ident
    assert isinstance(parse_log_line(line.value), Ok), ident
