"""spec lane 22, tests 1-3: a scripted thread's first turn saved without a snapshot or a sandbox,
then run by run_evals offline: zero model calls, no effect, and a changed recorded result fails."""

import asyncio
import json
from pathlib import Path

from eval_kit import Refunds, saved

from threads import run_evals
from threads.loop import guard
from threads.reduce.handlers import to_json

LOOKUP_RESULT = 5
"""The lookup's tool_result: the first appended event a changed result makes differ."""
SANDBOX_V2 = 2


def test_a_first_turn_with_no_sandbox_saves_portable_and_passes_offline(tmp_path: Path) -> None:
    case = asyncio.run(saved(tmp_path))
    assert (case.path, case.portable, case.reason) == (str(tmp_path / "refund-policy"), True, None)
    before, requests = Refunds.count, guard.requests_seen()
    report = asyncio.run(run_evals(cases=str(tmp_path)))
    assert (Refunds.count, guard.requests_seen()) == (before, requests)
    assert to_json(report.cases[0]) == {
        "name": "refund-policy",
        "status": "passed",
        "checks": {
            "replay": {"ok": True},
            "rerun": {
                "ok": True,
                "unmatched": [],
                "script_left": 0,
                "stubs_unmatched": 0,
                "unrecorded_calls": 0,
                "unrecorded_hooks": 0,
            },
        },
    }
    assert report.ok
    assert report.summary == (
        "1 passed, 0 failed (framework checks only; pass --agent to detect changes to your agents)"
    )
    files = sorted(p.name for p in (tmp_path / "refund-policy").iterdir())
    assert "sandbox.json" in files
    assert "line0.json" in files


def test_a_changed_read_only_result_fails_the_rerun(tmp_path: Path) -> None:
    asyncio.run(saved(tmp_path))
    path = tmp_path / "refund-policy" / "sandbox.json"
    sandbox = json.loads(path.read_text())
    assert sandbox["version"] == SANDBOX_V2
    sandbox["results"][0]["preview"] = "order 42: never shipped"
    path.write_text(json.dumps(sandbox))
    report = asyncio.run(run_evals(cases=str(tmp_path)))
    case = to_json(report.cases[0])
    assert isinstance(case, dict)
    assert case["status"] == "failed"
    assert case["checks"]["rerun"]["mismatch"] == {  # type: ignore[index] - JSON test read
        "index": LOOKUP_RESULT,
        "want": "tool_result",
        "got": "tool_result",
    }
    assert not report.ok


def test_a_typescript_saved_case_passes_in_python() -> None:
    """Cross-language: the corpus's saved cases are TypeScript bytes (spec/conformance/evals)."""
    root = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "evals" / "eval-pass"
    report = asyncio.run(run_evals(cases=str(root / "cases")))
    assert [c.status for c in report.cases] == ["passed"]
