"""The reference validator for semantic rules 31-45 (spec/tools/fixtures/ref_team.py) agrees with
every committed case, and fails a case whose expected result contradicts a rule."""

import json
import shutil
from pathlib import Path

from fixtures.common import CASES, STAGED
from fixtures.ref_team import ref_check


def test_every_committed_case_agrees_with_the_reference_rules() -> None:
    assert ref_check(CASES, STAGED) == []


def test_a_case_that_accepts_a_broken_rule_is_reported(tmp_path: Path) -> None:
    case = tmp_path / "woken-mixed-runs-rejected"
    shutil.copytree(CASES / case.name, case)
    (case / "expected.json").write_text(json.dumps({"outcome": "ok"}))
    assert ref_check(tmp_path) == [
        "reference validator: woken-mixed-runs-rejected: accepted, but log@26 breaks 32: woken "
        "causes are not one run's children of this append"
    ]


def test_an_old_log_with_a_late_result_in_another_runs_turn_is_accepted() -> None:
    """A deferred result landing in a later run's open turn is ordinary input (Gate 1 §2.7.3)."""
    for name in ("render-reserved-control-events", "legacy-late-result-in-another-runs-turn"):
        assert (CASES / name).is_dir()
    assert ref_check(CASES) == []
