"""A live eval never performs a real side effect (spec lane 22, C.1; review H1 and H2): every
effectful call of the run tree answers from the case's recordings, a handoff target's too, and the
sandbox's own tools run for real only inside a sandbox whose egress is deny-all."""

import asyncio
from dataclasses import replace
from pathlib import Path

from eval_kit import ALLOW, LOOKUP, REFUND, REFUND_TURN, Refunds, saved, say, use, verdicts_reply
from local_sandbox import LOCAL_INFO, LocalSandbox

from threads import EvalReport, Live, agent, run_evals, scripted_model
from threads.log import Budget

RUBRIC = ("Quotes the 30-day refund window",)
BUDGET = Budget(max_model_requests=10)
BASH = use("bash", {"command": "touch ran-for-real"}, "s1")


def _save(tmp: Path) -> Path:
    """The refund case, saved first: its recording ran the refund once, before any eval."""
    cases = tmp / "cases"
    asyncio.run(saved(cases, rubric=RUBRIC))
    return cases


def _live(cases: Path, bot: object) -> EvalReport:
    live = Live(scripted_model({"responses": [verdicts_reply([True])]}), BUDGET)
    return asyncio.run(run_evals(cases=str(cases), agents=[bot], live=live))  # pyright: ignore[reportArgumentType] - any agent handle


def test_a_handoff_targets_effects_answer_from_the_recordings(tmp_path: Path) -> None:
    billing = agent(
        name="billing",
        model=scripted_model(
            {"responses": [use("issue_refund", {"id": "99"}, "b1"), say("Done.")]}
        ),
        tools=[REFUND],
        permissions=ALLOW.model_copy(update={"allow": ["issue_refund"]}),
    )
    support = agent(
        name="support",
        instructions="You handle refunds.",
        model=scripted_model({"responses": [use("handoff", {"agent": "billing"}, "h1")]}),
        tools=[LOOKUP, REFUND],
        permissions=ALLOW,
        handoffs=[billing],
    )
    cases = _save(tmp_path)
    before = Refunds.count
    report = _live(cases, support)
    assert Refunds.count == before
    assert report.cases[0].status == "error"


def test_a_team_lead_is_never_run_live(tmp_path: Path) -> None:
    """Its members run in the team worker, where the case's stubs can't reach."""
    billing = agent(name="billing", model=scripted_model({"responses": []}), tools=[REFUND])
    lead = agent(
        name="support",
        instructions="You handle refunds.",
        model=scripted_model({"responses": list(REFUND_TURN)}),
        tools=[LOOKUP, REFUND],
        permissions=ALLOW,
        team=[billing],
    )
    report = _live(_save(tmp_path), lead)
    assert (report.cases[0].status, report.cases[0].reason) == (
        "skipped",
        "live_not_runnable:team_calls",
    )
    assert (report.model_calls.agent, report.model_calls.judge) == (0, 0)


def _bashing(box: LocalSandbox, *, unenforced: bool) -> object:
    permissions = ALLOW.model_copy(update={"allow": ["lookup_order", "issue_refund", "bash"]})
    responses = [BASH, *REFUND_TURN]
    if unenforced:
        return agent(
            name="support",
            instructions="You handle refunds.",
            model=scripted_model({"responses": responses}),
            tools=[LOOKUP, REFUND],
            permissions=permissions,
            sandbox=box,
            egress="unenforced",
        )
    return agent(
        name="support",
        instructions="You handle refunds.",
        model=scripted_model({"responses": responses}),
        tools=[LOOKUP, REFUND],
        permissions=permissions,
        sandbox=box,
    )


def test_with_deny_all_egress_sandbox_tools_run_for_real(tmp_path: Path) -> None:
    root = tmp_path / "box"
    root.mkdir()
    report = _live(_save(tmp_path), _bashing(LocalSandbox(root), unenforced=False))
    assert report.cases[0].checks.judge is not None
    assert any(root.rglob("ran-for-real"))


def test_without_deny_all_egress_a_sandbox_tool_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "box"
    root.mkdir()
    open_box = LocalSandbox(root, replace(LOCAL_INFO, egress="unenforced"))
    report = _live(_save(tmp_path), _bashing(open_box, unenforced=True))
    c = report.cases[0]
    assert (c.status, c.reason) == ("error", "failed: unmatched_external_op")
    assert not any(root.rglob("ran-for-real"))
