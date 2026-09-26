"""The live check (spec lane 22, C, tests 6-7): the current agent answers the case input, a judge
grades the whole turn. Every model here is scripted; the global guard is on."""

import asyncio
from pathlib import Path

import pytest
from eval_kit import (
    ALLOW,
    LOOKUP,
    REFUND,
    REFUND_TURN,
    Refunds,
    priced,
    saved,
    say,
    support,
    use,
    verdicts_reply,
)
from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads import Agent, ConfigError, EvalReport, Live, agent, run_evals, sqlite
from threads.agents.store import Store, scoped
from threads.evals.judge import judge_input
from threads.log import Budget, Event, ThreadId, UnknownEvent, UserInputEvent
from threads.result import Ok
from threads.thread.handle import open_thread

RUBRIC = ("Quotes the 30-day refund window", "Looks up the order before refunding")
BUDGET = Budget(max_model_requests=10)


def live_run(  # noqa: PLR0913 - one test double per knob
    tmp: Path,
    bot: Agent[None, str],
    judge: list[JsonValue],
    *,
    rubric: tuple[str, ...] | None = RUBRIC,
    store: Store | None = None,
    budget: Budget = BUDGET,
) -> EvalReport:
    async def body() -> EvalReport:
        await saved(tmp, rubric=rubric)
        live = Live(priced(judge, "judge-model"), budget)
        return await run_evals(cases=str(tmp), agents=[bot], live=live, store=store)

    return asyncio.run(body())


async def events_of(store: Store, id: str) -> list[Event]:
    thread = await open_thread(scoped(store, "evals"), ThreadId(id))
    assert isinstance(thread, Ok), thread
    timeline = await thread.value.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries if not isinstance(e.event, UnknownEvent)]


def test_a_scripted_judge_grades_the_whole_turn_from_the_canonical_transcript(
    tmp_path: Path,
) -> None:
    store = sqlite(":memory:")
    report = live_run(tmp_path, support(REFUND_TURN), [verdicts_reply([True, True])], store=store)
    case = report.cases[0]
    assert case.status == "passed"
    judge = case.checks.judge
    assert judge is not MISSING
    assert [(v.criterion, v.text, v.pass_) for v in judge.verdicts] == [
        (1, RUBRIC[0], True),
        (2, RUBRIC[1], True),
    ]
    assert judge.score == 1
    assert (report.model_calls.agent, report.model_calls.judge) == (3, 1)
    assert isinstance(judge.thread_id, str)
    assert isinstance(judge.judge_thread_id, str)
    lead = asyncio.run(events_of(store, judge.thread_id))
    judged = asyncio.run(events_of(store, judge.judge_thread_id))
    asked = next(e for e in judged if isinstance(e, UserInputEvent))
    answer = "Refunded order 42; it is inside the 30-day window."
    assert asked.data.text == judge_input("Please refund order 42.", lead, answer, RUBRIC)
    assert report.summary.startswith(
        "1 passed, 0 failed; 4 model calls (3 agent, 0 user, 1 judge), "
    )


def test_one_failing_criterion_fails_and_the_default_store_keeps_no_ids(tmp_path: Path) -> None:
    report = live_run(tmp_path, support(REFUND_TURN), [verdicts_reply([True, False])])
    case = report.cases[0]
    assert (case.status, case.reason) == ("failed", "judge: criterion 2 failed")
    judge = case.checks.judge
    assert judge is not MISSING
    assert judge.score == 0.5  # noqa: PLR2004 - one of two
    assert judge.thread_id is MISSING
    assert not report.ok


def test_invalid_verdicts_twice_are_judge_invalid_never_a_pass(tmp_path: Path) -> None:
    bad = use("final_output", {"verdicts": [{"criterion": 1, "pass": True, "reason": "ok"}]}, "v1")
    report = live_run(tmp_path, support(REFUND_TURN), [bad, bad])
    assert (report.cases[0].status, report.cases[0].reason) == ("error", "judge_invalid")


def test_a_case_with_no_criteria_is_skipped_as_no_rubric(tmp_path: Path) -> None:
    report = live_run(tmp_path, support(REFUND_TURN), [], rubric=None)
    assert (report.cases[0].status, report.cases[0].reason) == ("skipped", "no_rubric")
    assert (report.model_calls.agent, report.model_calls.judge) == (0, 0)


def test_an_effect_with_unrecorded_arguments_fails_closed(tmp_path: Path) -> None:
    before = Refunds.count
    other = [use("lookup_order", {"id": "42"}, "c1"), use("issue_refund", {"id": "43"}, "c2")]
    report = live_run(tmp_path, support([*other, say("Done.")]), [])
    # Only the recording, when the case was saved, refunded: the live run never did.
    assert Refunds.count == before + 1
    assert (report.cases[0].status, report.cases[0].reason) == (
        "error",
        "failed: unmatched_external_op",
    )


def test_an_approval_parks_the_run(tmp_path: Path) -> None:
    asks = ALLOW.model_copy(update={"allow": ["lookup_order"], "ask": ["issue_refund"]})
    asking = agent(
        name="support", model=priced(REFUND_TURN), tools=[LOOKUP, REFUND], permissions=asks
    )
    report = live_run(tmp_path, asking, [])
    assert (report.cases[0].status, report.cases[0].reason) == ("error", "parked")


def test_the_budget_stops_a_run(tmp_path: Path) -> None:
    report = live_run(tmp_path, support(REFUND_TURN), [], budget=Budget(max_model_requests=1))
    assert (report.cases[0].status, report.cases[0].reason) == ("error", "budget_exhausted")


def test_live_without_agents_names_what_is_missing(tmp_path: Path) -> None:
    live = Live(priced([]), BUDGET)
    with pytest.raises(ConfigError) as raised:
        asyncio.run(run_evals(cases=str(tmp_path), agents=[], live=live))
    assert raised.value.message == "live evals need agents: pass agents"
    with pytest.raises(ConfigError) as missing:
        asyncio.run(run_evals(cases=str(tmp_path / "nothing")))
    assert missing.value.message.startswith("cases: no directory ")
