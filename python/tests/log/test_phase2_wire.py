"""Teams Phase 2 before the rest of its build (spec/conformance/README.md, "Staged cases"):
every case still under staged-phase-2 is a valid case whose lines all pass the line schema, and
a reader refuses its unbuilt form as unsupported_critical_event, never reducing it as ordinary
work. Since lane 29D that form is supervision's (rule 51): `supervisor_decided`, and the
`member_started{restart_of}` that follows a decision. The same checks as TypeScript's
test/log/phase2-wire.test.ts."""

import json
from pathlib import Path

import pytest
from schema_check import CASE_ID, valid

from threads.log import parse_log_line
from threads.result import Ok
from threads.store.verify import verify_export

STAGED = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "staged-phase-2"
REFUSED = "unsupported_critical_event"


def _read(log: Path) -> tuple[str, int]:
    verified = verify_export(log.read_bytes(), 0)
    if isinstance(verified, Ok):
        return ("ok", 0)
    return (verified.error.code, -1 if verified.error.seq is None else verified.error.seq)


@pytest.mark.parametrize("case", sorted(STAGED.iterdir()), ids=lambda p: p.name)
def test_a_phase_2_case_is_valid_its_lines_parse_and_its_forms_are_refused(case: Path) -> None:
    assert valid(json.loads((case / "case.json").read_text()), f"{CASE_ID}#/$defs/Case")
    assert valid(json.loads((case / "expected.json").read_text()), f"{CASE_ID}#/$defs/Expected")
    codes: list[str] = []
    for log in sorted((case / "logs").glob("*.jsonl")):
        for line in log.read_text(encoding="utf-8").splitlines():
            assert isinstance(parse_log_line(line), Ok), (log.name, line[:80])
        codes.append(_read(log)[0])
    assert set(codes) <= {"ok", REFUSED}
    assert REFUSED in codes


LOGS = STAGED / "host-supervisor-restart" / "logs"


def test_a_host_team_log_reads_up_to_its_first_supervisor_decision() -> None:
    """Its team_opened{kind: host} and its first member_started{host_member} are lane 29D's."""
    assert _read(LOGS / "team.jsonl") == (REFUSED, 3)


def test_a_host_members_log_reads_whole() -> None:
    assert _read(LOGS / "billing.jsonl") == ("ok", 0)
