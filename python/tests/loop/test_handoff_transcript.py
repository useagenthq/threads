"""A handoff keeps its tool context (spec/schema/README.md, "Handoff scope"): the forwarded
transcript carries the handing-off turn's tool calls and results, capped at the spill threshold
with the oldest dropped first. The shared vector pins the text."""

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from threads.agents.transcript import handoff_transcript
from threads.log import Event, Head, Header, UnknownEvent, parse_log_line
from threads.result import Ok

VECTOR: dict[str, JsonValue] = json.loads(
    (
        Path(__file__).resolve().parents[3] / "spec/conformance/vectors/handoff-transcripts.json"
    ).read_text(encoding="utf-8")
)


def _cases() -> list[tuple[str, int, list[str], str]]:
    cases = VECTOR["cases"]
    assert isinstance(cases, list)
    out: list[tuple[str, int, list[str], str]] = []
    for c in cases:
        assert isinstance(c, dict)
        name, cap, lines, text = c["name"], c["cap"], c["lines"], c["transcript"]
        assert isinstance(name, str)
        assert isinstance(cap, int)
        assert isinstance(lines, list)
        assert isinstance(text, str)
        out.append((name, cap, [str(line) for line in lines], text))
    return out


def _event(line: str) -> Event:
    parsed = parse_log_line(line)
    assert isinstance(parsed, Ok), parsed
    assert not isinstance(parsed.value, Header | Head | UnknownEvent)
    return parsed.value


@pytest.mark.parametrize(("name", "cap", "lines", "expected"), _cases())
def test_the_transcript_matches_the_vector(
    name: str, cap: int, lines: list[str], expected: str
) -> None:
    assert name
    assert handoff_transcript([_event(line) for line in lines], cap) == expected
