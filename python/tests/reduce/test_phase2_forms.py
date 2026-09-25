"""Each Teams Phase 2 form, one line of the shared team wire vector, is refused by this reader as
unsupported_critical_event until the Phase 2 build (spec/schema/README.md, "Teams Phase 2"). The
same forms as TypeScript's test/validate/phase2-forms.test.ts."""

import json
from pathlib import Path

import pytest

from threads.log import Event, Head, Header, UnknownEvent, parse_log_line
from threads.reduce import rules_phase2
from threads.result import Ok

VECTOR = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "vectors" / "team-wire.json"
LINES: dict[str, str] = {
    str(c["name"]): str(c["line"]) for c in json.loads(VECTOR.read_text(encoding="utf-8"))["cases"]
}
FORMS = [
    ("team_opened of a host team", "team_opened{kind: host}"),
    ("member_started of a host member", "member_started{host_member}"),
    ("thread_started of a host member", "thread_started{host_member}"),
    ("supervisor_decided", "supervisor_decided"),
    ("member_idle{turn_failed}", "member_idle{turn_failed}"),
    ("ask_closed failed", "ask_closed{failed}"),
    ("budget_exceeded of a hop cap", "budget_exceeded{scope: hop}"),
    ("a caller's ask", "a caller's mail"),
    ("a host member's reply to a caller", "mail to a caller"),
    ("a receipt to a caller", "mail to a caller"),
    ("a turn_failed bounce to a member", "a turn_failed bounce"),
]


def _event(name: str) -> Event:
    parsed = parse_log_line(LINES[name])
    assert isinstance(parsed, Ok), name
    line = parsed.value
    assert not isinstance(line, Header | Head | UnknownEvent), name
    return line


@pytest.mark.parametrize(("name", "form"), FORMS, ids=[n for n, _ in FORMS])
def test_a_phase_2_form_is_refused(name: str, form: str) -> None:
    refused = rules_phase2.not_yet(_event(name))
    assert refused is not None
    assert refused.code == "unsupported_critical_event"
    assert refused.message.startswith(f"{form} is not reduced")


def test_a_phase_1_team_event_passes() -> None:
    assert rules_phase2.not_yet(_event("team_opened")) is None
