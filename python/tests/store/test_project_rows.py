"""The historical projector: rows from settlement events alone, never from the clock."""

from pathlib import Path

from threads.result import Ok
from threads.store import verify_export
from threads.store.project_rows import project_rows

CASES = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "cases"
FAR_FUTURE = 4_000_000_000_000


def _rows(case: str) -> tuple[list[str], list[str]]:
    verified = verify_export((CASES / case / "log.jsonl").read_bytes(), FAR_FUTURE)
    assert isinstance(verified, Ok)
    rows = project_rows(verified.value.fold.events)
    return [a.state for a in rows.approvals], [q.state for q in rows.questions]


def test_a_grant_stays_granted_long_after_its_challenge_expired() -> None:
    assert _rows("permission-thread-rule-added") == (["granted"], [])


def test_questions_are_open_answered_or_expired_by_their_settlement() -> None:
    assert _rows("ask-user-rejected-answer-keeps-question-open") == ([], ["open"])
    assert _rows("ask-user-parks-then-answered") == ([], ["answered"])
