"""The model-request guard as a value (spec lane 22, C.7, tests 4b and 8): a live model the guard
blocks stops the run, which returns the report with `aborted`; no request is sent. Model totals
count subagents, and cost sums the tree costs (C.6)."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from eval_kit import ALLOW, LOOKUP, REFUND, REFUND_TURN, priced, saved, say, support, use
from eval_kit import verdicts_reply as verdicts
from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads import Live, ModelBlockedError, agent, run_evals, scripted_model, sqlite
from threads.agents.store import scoped
from threads.log import Budget, ThreadId
from threads.loop import guard
from threads.loop.model import ModelChunk, ModelContext, ModelInfo, ModelRequest
from threads.result import Ok
from threads.thread.handle import open_thread

RUBRIC = ("Quotes the 30-day refund window",)
BUDGET = Budget(max_model_requests=10)


class Real:
    """A model that is not the scripted test kit, counting the requests that reach it."""

    def __init__(self, responses: Sequence[JsonValue]) -> None:
        self._inner = scripted_model({"responses": list(responses)})
        self.sent = 0

    @property
    def info(self) -> ModelInfo:
        return self._inner.info

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        self.sent += 1
        async for chunk in self._inner.send(request, context):
            yield chunk


def test_model_request_blocked_is_a_runtime_error_and_names_the_model() -> None:
    error = ModelBlockedError("anthropic/claude-haiku-4-5")
    assert isinstance(error, RuntimeError)
    assert error.model == "anthropic/claude-haiku-4-5"


def test_a_blocked_judge_errors_that_case_and_leaves_the_rest_not_run(tmp_path: Path) -> None:
    async def body() -> None:
        await saved(tmp_path, "a-refund", RUBRIC)
        await saved(tmp_path, "b-refund", RUBRIC)
        judge = Real([verdicts([True])])
        report = await run_evals(
            cases=str(tmp_path),
            agents=[support([*REFUND_TURN, *REFUND_TURN])],
            live=Live(judge, BUDGET),
        )
        assert report.aborted is not MISSING
        assert (report.aborted.code, report.aborted.case, report.aborted.model) == (
            "model_blocked",
            "a-refund",
            "scripted/scripted-1",
        )
        assert [(c.name, c.status) for c in report.cases] == [
            ("a-refund", "error"),
            ("b-refund", "not_run"),
        ]
        assert report.cases[0].reason == "model_blocked"
        assert judge.sent == 0
        assert not report.ok

    asyncio.run(body())


def test_a_blocked_model_on_a_background_subagent_aborts_the_eval(tmp_path: Path) -> None:
    async def body() -> None:
        await saved(tmp_path, rubric=RUBRIC)
        child = Real([say("never")])
        reviewer = agent(name="reviewer", model=child)
        spawn = use(
            "spawn_agent", {"agent": "reviewer", "prompt": "Check.", "background": True}, "c0"
        )
        lead = agent(
            name="support",
            model=scripted_model({"responses": [spawn, say("Started a review.")]}),
            subagents=[reviewer],
        )
        report = await run_evals(
            cases=str(tmp_path), agents=[lead], live=Live(scripted_model({"responses": []}), BUDGET)
        )
        assert report.aborted is not MISSING
        assert report.aborted.code == "model_blocked"
        assert child.sent == 0

    asyncio.run(body())


def test_model_calls_count_a_subagent_and_cost_sums_the_tree_costs(tmp_path: Path) -> None:
    async def body() -> None:
        await saved(tmp_path, rubric=RUBRIC)
        reviewer = agent(name="reviewer", model=priced([say("Looks right.")], "reviewer-model"))
        spawn = use("spawn_agent", {"agent": "reviewer", "prompt": "Check the refund."}, "c0")
        lead = agent(
            name="support",
            model=priced([spawn, *REFUND_TURN]),
            tools=[LOOKUP, REFUND],
            permissions=ALLOW,
            subagents=[reviewer],
        )
        store = sqlite(":memory:")
        live = Live(priced([verdicts([True])], "judge-model"), BUDGET)
        report = await run_evals(cases=str(tmp_path), agents=[lead], live=live, store=store)
        judge = report.cases[0].checks.judge
        assert judge is not MISSING
        # The judge passed it; the agent's config changed since the case was saved: stale.
        assert (report.cases[0].status, judge.score) == ("stale", 1)
        assert (report.model_calls.agent, report.model_calls.judge) == (5, 1)
        total = 0
        for id in (judge.thread_id, judge.judge_thread_id):
            assert isinstance(id, str)
            thread = await open_thread(scoped(store, "evals"), ThreadId(id))
            assert isinstance(thread, Ok)
            cost = await thread.value.cost(tree=True)
            assert isinstance(cost, Ok)
            assert cost.value
            total += cost.value.known_nanos
        assert report.cost
        assert report.cost.known_nanos == total > 0

    asyncio.run(body())


def test_an_offline_run_counts_no_real_model_request(tmp_path: Path) -> None:
    asyncio.run(saved(tmp_path))
    before = guard.requests_seen()
    asyncio.run(run_evals(cases=str(tmp_path)))
    assert guard.requests_seen() == before
