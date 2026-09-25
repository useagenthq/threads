"""Teams Phase 2 before its build (spec/conformance/README.md, "Staged cases"): every case under
staged-phase-2 is a valid case whose lines all pass the line schema, and a reader refuses each
Phase 2 form as unsupported_critical_event, never reducing it as ordinary work. The same checks as
TypeScript's test/log/phase2-wire.test.ts."""

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


LOGS = STAGED / "host-caller-ask-answered" / "logs"


def test_the_host_team_log_is_refused_at_its_host_team_opened() -> None:
    assert _read(LOGS / "team.jsonl") == (REFUSED, 1)


def test_a_host_members_log_is_refused_at_its_host_member_thread_started() -> None:
    assert _read(LOGS / "billing.jsonl") == (REFUSED, 1)


def test_a_callers_log_reads_up_to_its_first_caller_mail() -> None:
    lines = (LOGS / "support.jsonl").read_text(encoding="utf-8").splitlines()
    sent = next(i for i, line in enumerate(lines) if '"type":"message_sent"' in line)
    assert _read(LOGS / "support.jsonl") == (REFUSED, sent)
