"""The Teams Phase 2 forms whose sub-lane has not landed are refused by this reader as
unsupported_critical_event; the ones lane 29D built are reduced (spec/schema/README.md, "Teams
Phase 2"). The same forms as TypeScript's test/validate/phase2-forms.test.ts."""

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
FORMS = [("supervisor_decided", "supervisor_decided")]
"""Still refused: supervision is lane 29E's (semantic rule 51)."""

BUILT = [
    "team_opened of a host team",
    "member_started of a host member",
    "thread_started of a host member",
    "member_idle{turn_failed}",
    "ask_closed failed",
    "budget_exceeded of a hop cap",
    "a caller's ask",
    "a host member's reply to a caller",
    "a receipt to a caller",
    "a turn_failed bounce to a member",
]
"""Reduced since lane 29D: host teams, host members, callers and turn failures."""


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


@pytest.mark.parametrize("name", BUILT)
def test_a_form_lane_29d_built_is_reduced(name: str) -> None:
    assert rules_phase2.not_yet(_event(name)) is None


def test_a_restart_waits_for_supervision() -> None:
    """A host member's start is reduced, but the restart that gives it a generation is 29E's."""
    restart = _event("member_started of a host member restarting generation 1")
    refused = rules_phase2.not_yet(restart)
    assert refused is not None
    assert refused.message.startswith("member_started{restart_of} is not reduced")


def test_a_phase_1_team_event_passes() -> None:
    assert rules_phase2.not_yet(_event("team_opened")) is None
