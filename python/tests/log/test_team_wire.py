"""The team wire contract before its build: the shared line vector parses exactly as authored,
and every staged case is a valid case whose lines all pass the line schema
(spec/conformance/README.md, "Staged cases")."""

import json
from pathlib import Path

import pytest
from pydantic import JsonValue
from schema_check import CASE_ID, valid

from threads.log import parse_log_line
from threads.result import Err, Ok
from threads.store import verify_export

SPEC = Path(__file__).resolve().parents[3] / "spec"
STAGED = SPEC / "conformance" / "staged"
VECTOR: dict[str, JsonValue] = json.loads(
    (SPEC / "conformance" / "vectors" / "team-wire.json").read_text(encoding="utf-8")
)


def _outcome(line: str) -> str:
    parsed = parse_log_line(line)
    return "event" if isinstance(parsed, Ok) else parsed.error.code


def _cases() -> list[tuple[str, str, bool]]:
    cases = VECTOR["cases"]
    assert isinstance(cases, list)
    out: list[tuple[str, str, bool]] = []
    for c in cases:
        assert isinstance(c, dict)
        name, line, ok = c["name"], c["line"], c["valid"]
        assert isinstance(name, str)
        assert isinstance(line, str)
        assert isinstance(ok, bool)
        out.append((name, line, ok))
    return out


@pytest.mark.parametrize(("name", "line", "ok"), _cases(), ids=[c[0] for c in _cases()])
def test_the_vector_line_parses_as_authored(name: str, line: str, ok: bool) -> None:
    assert _outcome(line) == ("event" if ok else "invalid_line"), name


def _logs(case: Path) -> list[Path]:
    single = case / "log.jsonl"
    return [single] if single.exists() else sorted((case / "logs").glob("*.jsonl"))


@pytest.mark.parametrize("case", sorted(STAGED.iterdir()), ids=lambda p: p.name)
def test_a_staged_case_is_valid_and_its_lines_pass_the_schema(case: Path) -> None:
    assert valid(json.loads((case / "case.json").read_text()), f"{CASE_ID}#/$defs/Case")
    assert valid(json.loads((case / "expected.json").read_text()), f"{CASE_ID}#/$defs/Expected")
    for log in _logs(case):
        for line in log.read_text(encoding="utf-8").splitlines():
            assert _outcome(line) != "invalid_line", (log.name, line[:80])


@pytest.mark.parametrize("label", ["lead", "researcher", "team"])
def test_a_team_log_is_refused_until_the_build(label: str) -> None:
    """Until the Teams build, a reader refuses a team log at its first team event or team form
    (a lead's thread_started{team}, a member's team_member parent, a team log's team_opened)."""
    log = (STAGED / "team-settle-wakes-lead" / "logs" / f"{label}.jsonl").read_bytes()
    verified = verify_export(log, 0)
    assert isinstance(verified, Err)
    assert (verified.error.code, verified.error.seq) == ("unsupported_critical_event", 1)
