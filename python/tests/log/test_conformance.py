"""Schema-level conformance: every line of every case log parses, except the ones a case breaks.

A case breaks a line on purpose in two ways (spec/conformance/README.md):
- its expected error is a line-level code (`invalid_line`, `unsupported_format`,
  `unsupported_critical_event`) at `error.seq`: exactly that line fails, with that code;
- its log ends without a newline: the last chunk is a torn write. Newline is the commit marker,
  so that chunk is dropped unparsed, whatever it holds (spec/schema/README.md wire rule 14).
Semantic errors (`invalid_transition`, `prev_hash_mismatch`, ...) are later stages, so those logs
must parse in full here.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from threads.log import Head, Header, LogLine, ParseError, UnknownEvent, parse_log_line
from threads.result import Err, Ok

CASES = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "cases"
MIN_CASES = 70
SCHEMA_LEVEL_CODES = frozenset({"invalid_line", "unsupported_critical_event", "unsupported_format"})
CASES_WITH_LOGS = sorted(d.name for d in CASES.iterdir() if (d / "log.jsonl").exists())


def test_corpus_is_present() -> None:
    assert len(CASES_WITH_LOGS) > MIN_CASES


def next_position(line: LogLine, position: int) -> int:
    if isinstance(line, Head):
        return position
    if isinstance(line, Header):
        return max(position, 1)
    return line.seq + 1


def parse_failures(log: bytes) -> list[tuple[bytes, ParseError]]:
    failures: list[tuple[bytes, ParseError]] = []
    position = 0  # an unreadable line's seq is its predecessor's plus 1 (conformance README)
    for raw in log.split(b"\n"):
        if not raw:
            continue
        match parse_log_line(raw.decode("utf-8")):
            case Err(error=error):
                seq: int = position if error.seq is None else error.seq
                failures.append((raw, replace(error, seq=seq)))
                position = seq + 1
            case Ok(value=line):
                position = next_position(line, position)
    return failures


def expected_failure(case: Path) -> tuple[str, int | None] | None:
    error = json.loads((case / "expected.json").read_text(encoding="utf-8")).get("error")
    if error is not None and error["code"] in SCHEMA_LEVEL_CODES:
        return error["code"], error.get("seq")
    return None


@pytest.mark.parametrize("name", CASES_WITH_LOGS)
def test_case_log_parses(name: str) -> None:
    case = CASES / name
    log = (case / "log.jsonl").read_bytes()
    log = log[: log.rfind(b"\n") + 1]  # drop a torn final chunk
    failures = parse_failures(log)
    expected = expected_failure(case)
    if expected is None:
        assert failures == []
        return
    code, seq = expected
    assert len(failures) == 1, failures
    raw, error = failures[0]
    assert error.code == code
    if seq is not None:
        # A header line has no seq; its errors carry seq 0 (schema README wire rule 8).
        assert error.seq == seq
    else:
        assert log.endswith(raw)


def test_unknown_non_critical_event_is_kept() -> None:
    log = (CASES / "unknown-critical-event-refuses" / "log.jsonl").read_bytes()
    parsed = [parse_log_line(raw.decode("utf-8")) for raw in log.split(b"\n") if raw]
    kept = [r.value for r in parsed if isinstance(r, Ok) and isinstance(r.value, UnknownEvent)]
    assert [(event.type, event.critical) for event in kept] == [("telemetry_ping", False)]
