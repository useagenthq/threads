"""The prefix of a simulated case (spec lane 32, B.1, tests 4 and 5): continued when the agent's
config_hash is unchanged (the real earlier turns are imported and the thread goes on, and the live
stub queue starts at the saved turn's entries), re-driven when it changed."""

import asyncio
import json
from pathlib import Path

from eval_kit import ALLOW, priced, saved_turns, say, use, verdicts_reply
from pydantic import BaseModel, JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads import Agent, EvalReport, Live, RunContext, agent, run_evals, scripted_model, sqlite
from threads import tool as make_tool
from threads.agents.store import Store, scoped
from threads.log import Budget, ModelRequestEvent, ThreadId, ToolResultEvent, UserInputEvent
from threads.result import Ok
from threads.thread.case_simulate import Simulate
from threads.thread.handle import open_thread

RUBRIC = ("The agent stays inside the refund policy",)
BUDGET = Budget(max_model_requests=40)
SCRIPT: Simulate = {"kind": "script", "messages": ["Thanks."]}
REFUND_MUST: dict[str, JsonValue] = {"type": "tool_call", "data": {"name": "issue_refund"}}


class Order(BaseModel):
    id: str


class Issued:
    """Every refund this process really issued, and a distinct result per call."""

    count = 0


async def _counting_refund(args: Order, _ctx: RunContext[None]) -> str:
    Issued.count += 1
    return f"refund #{Issued.count} for {args.id}"


COUNTING = make_tool(
    name="issue_refund",
    description="Refund an order.",
    input=Order,
    runs="host",
    effect="unguarded",
    execute=_counting_refund,
)


def refunder(
    responses: list[JsonValue], instructions: str = "You handle refunds."
) -> Agent[None, str]:
    return agent(
        name="support",
        instructions=instructions,
        model=scripted_model({"responses": responses}),
        tools=[COUNTING],
        permissions=ALLOW,
    )


def recorded() -> list[JsonValue]:
    """Two recorded turns, each refunding order 42: the same call, two different results."""
    return [
        use("issue_refund", {"id": "42"}, "p1"),
        say("Refunded it the first time."),
        use("issue_refund", {"id": "42"}, "c1"),
        say("Refunded it again."),
    ]


async def _seen(store: Store, thread_id: str) -> tuple[list[str], list[str], int]:
    opened = await open_thread(scoped(store, "evals"), ThreadId(thread_id))
    assert isinstance(opened, Ok), opened
    read = await opened.value.timeline()
    assert isinstance(read, Ok), read
    events = [e.event for e in read.value.entries]
    inputs = [
        e.data.text
        for e in events
        if isinstance(e, UserInputEvent) and isinstance(e.data.text, str)
    ]
    results = [e.data.preview for e in events if isinstance(e, ToolResultEvent)]
    requests = sum(1 for e in events if isinstance(e, ModelRequestEvent))
    return inputs, results, requests


def test_a_continued_prefix_answers_from_the_saved_turns_stub(tmp_path: Path) -> None:
    store = sqlite(":memory:")

    async def body() -> EvalReport:
        Issued.count = 0
        saved = await saved_turns(
            tmp_path,
            "continued",
            refunder(recorded()),
            ["Refund order 42.", "Do it once more."],
            must=REFUND_MUST,
            rubric=RUBRIC,
            simulate=SCRIPT,
        )
        stubs: JsonValue = json.loads((Path(saved.path) / "stubs.json").read_text())
        assert isinstance(stubs, dict)
        entries = stubs["stubs"]
        assert isinstance(entries, list)
        # One key, two entries, numbered in log order: the prefix's first.
        assert [e["occurrence"] for e in entries if isinstance(e, dict)] == [0, 1]
        assert [e["output"] for e in entries if isinstance(e, dict)] == [
            "refund #1 for 42",
            "refund #2 for 42",
        ]
        assert [e.get("scope") for e in entries if isinstance(e, dict)] == ["prefix", None]
        Issued.count = 0
        now = refunder(
            [
                use("issue_refund", {"id": "42"}, "q1"),
                say("Refunded it again."),
                say("You're welcome."),
            ]
        )
        live = Live(priced([verdicts_reply([True])], "judge-model"), BUDGET)
        return await run_evals(cases=str(tmp_path), agents=[now], live=live, store=store)

    report = asyncio.run(body())
    case = report.cases[0]
    assert case.simulation is not MISSING
    assert (case.simulation.prefix, case.simulation.prefix_turns) == ("continued", 1)
    assert (case.simulation.messages, case.simulation.ended) == (2, "script_done")
    assert case.status == "passed"
    assert Issued.count == 0
    assert case.checks.judge is not MISSING
    thread_id = case.checks.judge.thread_id
    assert isinstance(thread_id, str)
    inputs, results, requests = asyncio.run(_seen(store, thread_id))
    # The real input came from the import; only the opener and the script message ran.
    assert inputs == ["Refund order 42.", "Do it once more.", "Thanks."]
    # The prefix's own result is in the imported log; the live call took the saved turn's.
    assert results == ["refund #1 for 42", "refund #2 for 42"]
    # The imported prefix holds 2 requests; this conversation added 3.
    assert (report.model_calls.agent, requests) == (3, 5)


def test_a_redriven_prefix_answers_its_effect_from_the_prefix_stub(tmp_path: Path) -> None:
    store = sqlite(":memory:")

    async def body() -> EvalReport:
        Issued.count = 0
        await saved_turns(
            tmp_path,
            "redriven",
            refunder(recorded(), "You handled refunds, once."),
            ["Refund order 42.", "Do it once more."],
            must=REFUND_MUST,
            rubric=RUBRIC,
            simulate=SCRIPT,
        )
        Issued.count = 0
        # Different instructions: a different config_hash, so the prefix is re-driven.
        now = refunder([*recorded(), say("You're welcome.")], "You handle refunds now.")
        live = Live(priced([verdicts_reply([True])], "judge-model"), BUDGET)
        return await run_evals(cases=str(tmp_path), agents=[now], live=live, store=store)

    report = asyncio.run(body())
    case = report.cases[0]
    assert case.simulation is not MISSING
    assert (case.simulation.prefix, case.simulation.prefix_turns) == ("redriven", 1)
    assert (case.simulation.messages, case.simulation.ended) == (2, "script_done")
    # The changed instructions are real drift, so the judged pass reads stale.
    assert (case.status, case.reason) == ("stale", "drift: prompt")
    # Both refunds answered from stubs: the effect never ran again.
    assert Issued.count == 0
    assert case.checks.judge is not MISSING
    thread_id = case.checks.judge.thread_id
    assert isinstance(thread_id, str)
    inputs, results, _ = asyncio.run(_seen(store, thread_id))
    assert inputs == ["Refund order 42.", "Do it once more.", "Thanks."]
    # The queue starts at the prefix entry, so the re-driven turn gets it.
    assert results == ["refund #1 for 42", "refund #2 for 42"]
