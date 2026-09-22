"""Schema-level conformance: every line of every case log parses, except the ones a case breaks.

A case breaks a line on purpose in two ways (spec/conformance/README.md):
- its expected error is a line-level code (`invalid_line`, `unsupported_format`,
  `unsupported_critical_event`) at `error.seq`: exactly that line fails, with that code;
- its log ends without a newline: the last chunk is a torn write and fails as `invalid_line`.
Semantic errors (`invalid_transition`, `prev_hash_mismatch`, ...) are later stages, so those logs
must parse in full here.
"""

import json
from pathlib import Path

import pytest

from threads.log import ParseError, UnknownEvent, parse_log_line
from threads.result import Err, Ok

CASES = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "cases"
MIN_CASES = 70
SCHEMA_LEVEL_CODES = frozenset({"invalid_line", "unsupported_critical_event", "unsupported_format"})
CASES_WITH_LOGS = sorted(d.name for d in CASES.iterdir() if (d / "log.jsonl").exists())


def test_corpus_is_present() -> None:
    assert len(CASES_WITH_LOGS) > MIN_CASES


def parse_failures(log: bytes) -> list[tuple[bytes, ParseError]]:
    failures: list[tuple[bytes, ParseError]] = []
    for raw in log.split(b"\n"):
        if not raw:
            continue
        match parse_log_line(raw.decode("utf-8")):
            case Err(error=error):
                failures.append((raw, error))
            case Ok():
                pass
    return failures


def expected_failure(case: Path, log: bytes) -> tuple[str, int | None] | None:
    error = json.loads((case / "expected.json").read_text(encoding="utf-8")).get("error")
    if error is not None and error["code"] in SCHEMA_LEVEL_CODES:
        return error["code"], error.get("seq")
    if not log.endswith(b"\n"):
        return "invalid_line", None
    return None


@pytest.mark.parametrize("name", CASES_WITH_LOGS)
def test_case_log_parses(name: str) -> None:
    case = CASES / name
    log = (case / "log.jsonl").read_bytes()
    failures = parse_failures(log)
    expected = expected_failure(case, log)
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
