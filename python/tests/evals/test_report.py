"""The CLI's per-case line and the cost figure in the summary (spec lane 22, D)."""

from typing import TYPE_CHECKING

from threads.evals.report import case_line, dollars

if TYPE_CHECKING:
    from pydantic import JsonValue


def test_a_stale_case_names_its_drift_once_then_what_was_left_unchecked() -> None:
    drift: JsonValue = {"ok": False, "kinds": ["model"], "unchecked": ["mcp:jira", "extension:crm"]}
    stale: dict[str, JsonValue] = {
        "name": "c",
        "status": "stale",
        "reason": "drift: model",
        "checks": {"drift": drift},
    }
    assert case_line(stale) == "STALE c drift: model; unchecked mcp:jira, extension:crm"
    ok: JsonValue = {"ok": True, "kinds": [], "unchecked": ["memory"]}
    passed: dict[str, JsonValue] = {"name": "c", "status": "passed", "checks": {"drift": ok}}
    assert case_line(passed) == "PASS c (drift: unchecked memory)"


def test_dollars_round_half_up_to_the_cent() -> None:
    got = [dollars(0), dollars(4_999_999), dollars(5_000_000), dollars(380_000_000)]
    assert got == ["$0.00", "$0.00", "$0.01", "$0.38"]
