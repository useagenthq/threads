"""A failed drill has to say why. A worker that ends before its pause point (a crash in setup, a
bad interpreter, a host that finished first) reports nothing on stdout, so the drill's own message
is the only evidence there is: it carries how the worker exited, what it had made durable and
what it printed, or the next occurrence is as undiagnosable as the last."""

from pathlib import Path

import pytest
from jobs.drill import WAIT_S, spawn, wait_at

pytestmark = pytest.mark.jobs

BOOM = "ModuleNotFoundError: No module named 'psycopg'"


def test_a_worker_that_ends_before_its_point_reports_how_it_ended(tmp_path: Path) -> None:
    script = tmp_path / "early_exit.py"
    script.write_text(f"import sys\n\nsys.exit({BOOM!r})\n")
    worker = spawn("serve", tmp_path, script=script)
    try:
        with pytest.raises(AssertionError) as ended:
            wait_at(worker, "effect_commit")
    finally:
        # A drill kills or finishes its workers; this one ended on its own, so it is waited on
        # here and reaping finds nothing left.
        worker.communicate(timeout=WAIT_S)

    said = str(ended.value)
    assert "ended before effect_commit" in said
    assert "exit code 1" in said
    assert BOOM in said, "the worker's stderr"
    assert "--- log" in said, "the durable log at the moment of failure"
