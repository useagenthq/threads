"""`threads eval`, in process through `main(argv)`, so the suite's model-request guard covers it
(spec lane 22, test 10). No test starts a `threads` subprocess."""

import asyncio
import json
from pathlib import Path

import pytest
from cli.eval_modules.eval_agents import CONNECTS, support
from cli.eval_modules.eval_simulated import support as simulated

from threads import CaseExpectation, sqlite
from threads.cli import main
from threads.result import Ok

MODULES = Path(__file__).resolve().parent / "eval_modules"
NOTE = " (framework checks only; pass --agent to detect changes to your agents)"


def cases_with(folder: Path, *names: str) -> str:
    async def body() -> None:
        for name in names:
            ran = await support().run("Where is order 42?", store=sqlite(":memory:"))
            must = CaseExpectation(must=({"type": "tool_call", "data": {"name": "lookup_order"}},))
            saved = await ran.thread.save_case(
                name,
                expect=must,
                external_effects="stub",
                rubric=("Quotes the 30-day window",),
                dir=str(folder),
            )
            assert isinstance(saved, Ok), saved

    asyncio.run(body())
    return str(folder)


def cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    got = capsys.readouterr()
    return code, got.out, got.err


def test_without_agent_framework_checks_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = cases_with(tmp_path, "a-case", "b-case")
    out = tmp_path / "report.json"
    code, printed, _ = cli(["eval", "--cases", folder, "--out", str(out)], capsys)
    assert code == 0
    assert printed == f"PASS a-case\nPASS b-case\n2 passed, 0 failed{NOTE}\n"
    report = out.read_text()
    assert report.endswith("}\n")
    assert json.loads(report)["summary"] == f"2 passed, 0 failed{NOTE}"


def test_case_filters_and_a_failing_case_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = cases_with(tmp_path, "a-case", "b-case")
    path = tmp_path / "b-case" / "sandbox.json"
    sandbox = json.loads(path.read_text())
    sandbox["results"][0]["preview"] = "order 42: lost"
    path.write_text(json.dumps(sandbox))
    assert cli(["eval", "--cases", folder, "--case", "a-case"], capsys)[0] == 0
    code, printed, _ = cli(["eval", "--cases", folder], capsys)
    assert code == 1
    assert "FAIL b-case rerun: event 5 is tool_result, recorded tool_result" in printed


def test_agent_adds_drift_and_stale_fails_only_with_strict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = cases_with(tmp_path, "a-case")
    same = cli(["eval", "--cases", folder, "--agent", str(MODULES / "eval_agents.py")], capsys)
    assert same[:2] == (0, "PASS a-case\n1 passed, 0 failed\n")
    mcp = cli(["eval", "--cases", folder, "--agent", str(MODULES / "eval_mcp.py")], capsys)
    assert mcp[0] == 0
    assert "PASS a-case (drift: unchecked mcp:jira)" in mcp[1]
    assert CONNECTS == [0]
    changed = str(MODULES / "eval_changed.py")
    drifted = cli(["eval", "--cases", folder, "--agent", changed], capsys)
    assert drifted[:2] == (
        0,
        "STALE a-case drift: prompt, tools (-lookup_order)\n0 passed, 0 failed, 1 stale\n",
    )
    assert cli(["eval", "--cases", folder, "--agent", changed, "--strict"], capsys)[0] == 1


def test_a_module_that_raises_at_import_is_exit_2_and_live_needs_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = cases_with(tmp_path, "a-case")
    thrown = cli(["eval", "--cases", folder, "--agent", str(MODULES / "eval_throws.py")], capsys)
    assert thrown[0] == 2  # noqa: PLR2004 - a usage error
    assert "eval_throws.py: import failed: JIRA_MCP_URL_FOR_THREADS_TESTS is not set" in thrown[2]
    live = cli(["eval", "--cases", folder, "--live"], capsys)
    assert (live[0], live[2]) == (2, "threads eval --live needs --agent <module>\n")
    no_judge = str(MODULES / "eval_no_judge.py")
    missing = cli(["eval", "--cases", folder, "--live", "--agent", no_judge], capsys)
    assert missing[0] == 2  # noqa: PLR2004 - a usage error
    assert "export judge from" in missing[2]
    assert cli(["eval", "--cases", str(tmp_path / "nope")], capsys)[0] == 2  # noqa: PLR2004


def test_live_grades_with_the_modules_judge_and_keeps_no_threads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = cases_with(tmp_path, "a-case")
    agents = str(MODULES / "eval_agents.py")
    code, printed, _ = cli(["eval", "--cases", folder, "--live", "--agent", agents], capsys)
    assert code == 0
    assert printed.split("\n") == [
        "live: 1 cases, up to 2 model runs (agent + judge), "
        'budget {"max_model_requests":10} per run',
        "PASS a-case",
        "judge threads were not kept; pass --store to keep them",
        "1 passed, 0 failed; 3 model calls (2 agent, 0 user, 1 judge), cost unknown",
        "",
    ]


def simulated_case(folder: Path) -> str:
    """One saved case whose live run is a conversation with a model playing the user."""

    async def body() -> None:
        ran = await simulated().run("Where is order 42?", store=sqlite(":memory:"))
        must = CaseExpectation(must=({"type": "tool_call", "data": {"name": "lookup_order"}},))
        saved = await ran.thread.save_case(
            "a-case",
            expect=must,
            external_effects="stub",
            rubric=("Quotes the 30-day window",),
            simulate={
                "kind": "model",
                "persona": "A polite but persistent customer.",
                "goal": "Get a refund, or a clear reason why not.",
                "max_messages": 3,
            },
            dir=str(folder),
        )
        assert isinstance(saved, Ok), saved

    asyncio.run(body())
    return str(folder)


def test_a_simulated_case_needs_the_module_to_export_user(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = simulated_case(tmp_path)
    plain = str(MODULES / "eval_agents.py")
    missing = cli(["eval", "--cases", folder, "--live", "--agent", plain], capsys)
    assert missing[0] == 2  # noqa: PLR2004 - a usage error
    assert missing[2] == f"export user from {plain}\n"
    agents = str(MODULES / "eval_simulated.py")
    code, printed, _ = cli(["eval", "--cases", folder, "--live", "--agent", agents], capsys)
    assert printed.split("\n")[0] == (
        "live: 1 cases (1 simulated, up to 3 user messages), "
        'budget {"max_model_requests":20} per conversation and per judge run'
    )
    assert code == 0
    assert "a-case: 2 messages, user done" in printed
