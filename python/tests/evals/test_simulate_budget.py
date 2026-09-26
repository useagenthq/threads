"""One budget per conversation (spec lane 32, B.5, test 6): `live.budget` covers every agent run and
every simulated-user run of the case, so each run is given what is left, field by field. A field the
author set whose remainder is <= 0 ends the case before the next run."""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from eval_kit import priced, saved_turns, say, support, use, verdicts_reply
from pydantic.experimental.missing_sentinel import MISSING

from threads import Agent, EvalReport, Live, run_evals, sqlite
from threads.agents.store import Store, scoped
from threads.evals.remaining_budget import Spent, remaining_budget
from threads.log import Budget, ThreadId, UserInputEvent
from threads.result import Ok
from threads.thread.case_simulate import Simulate

if TYPE_CHECKING:
    from pydantic import JsonValue
from threads.thread.handle import open_thread

RUBRIC = ("The agent stays inside the refund policy",)


def reads(turns: int) -> Agent[None, str]:
    """One turn of two requests: a read-only call, then the answer."""
    replies: list[JsonValue] = []
    for i in range(turns):
        replies.extend([use("lookup_order", {"id": "42"}, f"r{i}"), say("Looked it up.")])
    return support(replies)


def test_each_field_is_the_limit_less_what_the_conversation_used() -> None:
    left = remaining_budget(
        Budget(max_model_requests=4, max_turns=3, max_wall_ms=1000),
        Spent(requests=2, turns=1, wall_ms=400),
    )
    assert left == Budget(max_model_requests=2, max_turns=2, max_wall_ms=600)


def test_a_field_the_author_did_not_set_is_never_passed() -> None:
    left = remaining_budget(Budget(max_turns=2), Spent(requests=99, turns=1))
    assert left == Budget(max_turns=1)
    assert left is not None
    assert left.max_model_requests is MISSING


def test_a_remainder_below_one_is_exhausted_not_a_zero_limit() -> None:
    assert remaining_budget(Budget(max_model_requests=2), Spent(requests=2)) is None


def test_a_wall_clock_advanced_across_both_threads_counts_once() -> None:
    # 250 ms on the agent thread and 300 on the user thread: one monotonic clock, 550 spent.
    left = remaining_budget(Budget(max_wall_ms=1000), Spent(wall_ms=250 + 300))
    assert left == Budget(max_wall_ms=450)


def test_unknown_usage_counts_at_its_upper_bound() -> None:
    assert remaining_budget(Budget(max_cost_nanos=1000), Spent(cost_nanos=1000)) is None


def _run(
    folder: Path, simulate: Simulate, turns: int, budget: Budget, store: Store | None = None
) -> EvalReport:
    async def body() -> EvalReport:
        await saved_turns(
            folder,
            "case",
            reads(1),
            ["Please refund order 42."],
            rubric=RUBRIC,
            simulate=simulate,
        )
        live = Live(priced([verdicts_reply([True])], "judge-model"), budget)
        return await run_evals(cases=str(folder), agents=[reads(turns)], live=live, store=store)

    return asyncio.run(body())


def test_max_model_requests_is_spent_down_then_the_case_ends_budget_exhausted(
    tmp_path: Path,
) -> None:
    script: Simulate = {"kind": "script", "messages": ["And again?", "And again?"]}
    report = _run(tmp_path, script, 3, Budget(max_model_requests=4))
    case = report.cases[0]
    assert (case.status, case.reason) == ("error", "budget_exhausted")
    # The agent ran twice, two requests each; the third run had nothing left.
    assert (report.model_calls.agent, report.model_calls.user) == (4, 0)


def test_each_run_records_the_budget_it_was_given(tmp_path: Path) -> None:
    store = sqlite(":memory:")
    script: Simulate = {"kind": "script", "messages": ["And again?"]}
    report = _run(tmp_path, script, 2, Budget(max_model_requests=6), store)
    case = report.cases[0]
    assert case.checks.judge is not MISSING
    thread_id = case.checks.judge.thread_id
    assert isinstance(thread_id, str)

    async def budgets() -> list[Budget]:
        opened = await open_thread(scoped(store, "evals"), ThreadId(thread_id))
        assert isinstance(opened, Ok), opened
        read = await opened.value.timeline()
        assert isinstance(read, Ok), read
        given: list[Budget] = []
        for entry in read.value.entries:
            event = entry.event
            if isinstance(event, UserInputEvent) and isinstance(event.data.budget, Budget):
                given.append(event.data.budget)
        return given

    # The opener got the whole budget; the next run got what its two requests left.
    assert asyncio.run(budgets()) == [
        Budget(max_model_requests=6),
        Budget(max_model_requests=4),
    ]
    assert case.simulation is not MISSING
    assert (case.simulation.messages, case.simulation.ended) == (2, "script_done")


def test_a_turn_completed_the_agent_adds_is_charged_to_max_turns(tmp_path: Path) -> None:
    script: Simulate = {"kind": "script", "messages": ["And again?", "Once more?"]}
    report = _run(tmp_path, script, 3, Budget(max_turns=2))
    case = report.cases[0]
    # Two turns fit; the third run is refused before it starts.
    assert (case.status, case.reason) == ("error", "budget_exhausted")
