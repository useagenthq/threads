"""Simulated users (spec lane 32, tests 1-3, 7 and 8): the live conversation, who plays the user,
what ends it, and that an effect the recording doesn't have fails the case closed."""

import asyncio
import json
from pathlib import Path

import pytest
from eval_kit import (
    LOOKUP,
    REFUND,
    REFUND_TURN,
    Refunds,
    priced,
    saved_turns,
    say,
    support,
    use,
    user_replies,
    verdicts_reply,
)
from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads import Agent, EvalReport, Live, agent, run_evals, scripted_model, sqlite
from threads.agents.store import Store, scoped
from threads.evals.simulated_user import VisibleMessage, simulated_user_input
from threads.log import Budget, ThreadId, UserInputEvent
from threads.result import Ok
from threads.thread.case_simulate import Simulate
from threads.thread.handle import open_thread

RUBRIC = ("The agent stays inside the refund policy",)
BUDGET = Budget(max_model_requests=40)
PERSONA = "A customer who bought headphones 40 days ago. Polite but persistent."
GOAL = "Get a refund, or a clear reason why not."
LOOKUP_MUST: dict[str, JsonValue] = {"type": "tool_call", "data": {"name": "lookup_order"}}


def model_user(max_messages: int | None = None) -> Simulate:
    got: Simulate = {"kind": "model", "persona": PERSONA, "goal": GOAL}
    if max_messages is not None:
        got = {**got, "max_messages": max_messages}
    return got


def read_turn(n: int) -> list[JsonValue]:
    """One read-only turn: look the order up, then answer. A call id is used once per thread."""
    return [use("lookup_order", {"id": "42"}, f"r{n}"), say("Looked it up.")]


def reads(turns: int) -> Agent[None, str]:
    replies: list[JsonValue] = []
    for i in range(turns):
        replies.extend(read_turn(i))
    return support(replies)


async def _simulated(folder: Path, name: str, simulate: Simulate, turns: int = 1) -> None:
    await saved_turns(
        folder,
        name,
        reads(turns),
        ["Please refund order 42."],
        must=LOOKUP_MUST,
        rubric=RUBRIC,
        simulate=simulate,
    )


def _live(  # noqa: PLR0913 - one test double per knob
    folder: Path,
    simulate: Simulate,
    bot: Agent[None, str],
    user: list[JsonValue] | None = None,
    *,
    store: Store | None = None,
    name: str = "case",
    budget: Budget = BUDGET,
) -> EvalReport:
    async def body() -> EvalReport:
        await _simulated(folder, name, simulate)
        live = Live(
            priced([verdicts_reply([True])], "judge-model"),
            budget,
            user=None if user is None else priced(user, "user-model"),
        )
        return await run_evals(cases=str(folder), agents=[bot], live=live, store=store)

    return asyncio.run(body())


async def _first_input(store: Store, thread_id: str) -> str:
    """The first user_input of a thread: what the judge or the simulated user was asked."""
    opened = await open_thread(scoped(store, "evals"), ThreadId(thread_id))
    assert isinstance(opened, Ok), opened
    read = await opened.value.timeline()
    assert isinstance(read, Ok), read
    for entry in read.value.entries:
        event = entry.event
        if isinstance(event, UserInputEvent) and isinstance(event.data.text, str):
            return event.data.text
    return ""


def test_a_model_user_that_is_done_on_its_fourth_reply(tmp_path: Path) -> None:
    store = sqlite(":memory:")
    report = _live(
        tmp_path,
        model_user(),
        reads(4),
        user_replies(["What about a repair?", "Is there any exception?", "Understood.", True]),
        store=store,
        name="pushback",
    )
    case = report.cases[0]
    assert (case.status, case.reason) == ("passed", MISSING)
    assert case.simulation is not MISSING
    assert (case.simulation.messages, case.simulation.ended) == (4, "user_done")
    assert (case.simulation.prefix, case.simulation.prefix_turns) == ("none", 0)
    assert (report.model_calls.user, case.status) == (4, "passed")
    assert "pushback: 4 messages, user done" in report.summary
    # The judge sees every user message after the opener, in order (spec lane 32, D).
    assert case.checks.judge is not MISSING
    judge_thread = case.checks.judge.judge_thread_id
    assert isinstance(judge_thread, str)
    asked: JsonValue = json.loads(asyncio.run(_first_input(store, judge_thread)))
    assert isinstance(asked, dict)
    items = asked["transcript"]
    assert isinstance(items, list)
    users = [i for i in items if isinstance(i, dict) and i["kind"] == "user"]
    assert users == [
        {"kind": "user", "text": "What about a repair?"},
        {"kind": "user", "text": "Is there any exception?"},
        {"kind": "user", "text": "Understood."},
    ]
    assert asked["goal"] == GOAL


def test_the_first_input_is_the_whole_visible_conversation(tmp_path: Path) -> None:
    store = sqlite(":memory:")
    report = _live(
        tmp_path,
        model_user(2),
        reads(2),
        user_replies(["And the repair?", True]),
        store=store,
        name="seen",
    )
    case = report.cases[0]
    assert case.simulation is not MISSING
    thread_id = case.simulation.user_thread_id
    assert isinstance(thread_id, str)

    async def inputs() -> list[str]:
        opened = await open_thread(scoped(store, "evals"), ThreadId(thread_id))
        assert isinstance(opened, Ok), opened
        read = await opened.value.timeline()
        assert isinstance(read, Ok), read
        return [
            e.event.data.text
            for e in read.value.entries
            if isinstance(e.event, UserInputEvent) and isinstance(e.event.data.text, str)
        ]

    asked = asyncio.run(inputs())
    assert asked[0] == simulated_user_input(
        [
            VisibleMessage("user", "Please refund order 42."),
            VisibleMessage("agent", "Looked it up."),
        ]
    )
    assert asked[1] == simulated_user_input([VisibleMessage("agent", "Looked it up.")])


def test_a_user_that_never_finishes_stops_at_max_messages(tmp_path: Path) -> None:
    report = _live(
        tmp_path,
        model_user(3),
        reads(3),
        user_replies(["Still no.", "Really?", "Come on."]),
        name="endless",
    )
    case = report.cases[0]
    assert case.simulation is not MISSING
    assert (case.simulation.ended, case.simulation.messages) == ("max_messages", 3)
    # Three user messages, each answered by one agent run of two requests.
    assert (report.model_calls.agent, case.status) == (6, "passed")


def test_a_script_user_needs_no_user_model(tmp_path: Path) -> None:
    script: Simulate = {"kind": "script", "messages": ["It's order 1234.", "Then the repair."]}
    report = _live(tmp_path, script, reads(3), name="scripted")
    case = report.cases[0]
    assert case.simulation is not MISSING
    assert (case.simulation.messages, case.simulation.ended) == (3, "script_done")
    assert (report.model_calls.user, case.status) == (0, "passed")


def test_output_the_runner_cannot_accept_is_simulator_invalid(tmp_path: Path) -> None:
    # Output the schema rejects, twice: invalid after the one retry.
    bad: list[str | bool | dict[str, JsonValue]] = [{"message": "Hi."}, {"message": "Again."}]
    report = _live(tmp_path, model_user(), reads(2), user_replies(bad), name="bad-user")
    case = report.cases[0]
    assert (case.status, case.reason) == ("error", "simulator_invalid")


def test_an_agent_run_that_parks_is_an_error(tmp_path: Path) -> None:
    async def body() -> EvalReport:
        await saved_turns(
            tmp_path,
            "parks",
            support(REFUND_TURN),
            ["Please refund order 42."],
            must={"type": "tool_call", "data": {"name": "issue_refund"}},
            rubric=RUBRIC,
            simulate={"kind": "script", "messages": ["And order 43?"]},
        )
        # The agent now asks before refunding, and an eval answers no approval.
        asks = agent(
            name="support",
            instructions="You handle refunds.",
            model=scripted_model({"responses": list(REFUND_TURN)}),
            tools=[LOOKUP, REFUND],
            permissions={"allow": ["lookup_order"], "ask": ["issue_refund"]},
        )
        live = Live(priced([verdicts_reply([True])], "judge-model"), BUDGET)
        return await run_evals(cases=str(tmp_path), agents=[asks], live=live)

    report = asyncio.run(body())
    assert (report.cases[0].status, report.cases[0].reason) == ("error", "parked")


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("input", {}, "simulate_content_input"),
        ("simulate_blocked", "unsettled_effect", "unsettled_effect"),
    ],
)
def test_a_case_a_live_run_cannot_simulate_is_skipped(
    tmp_path: Path, field: str, value: JsonValue, reason: str
) -> None:
    async def body() -> EvalReport:
        await _simulated(tmp_path, "blocked", model_user())
        path = tmp_path / "blocked" / "case.json"
        meta: JsonValue = json.loads(path.read_text())
        assert isinstance(meta, dict)
        meta[field] = value
        path.write_text(json.dumps(meta, indent=2) + "\n")
        live = Live(
            priced([verdicts_reply([True])], "judge-model"),
            BUDGET,
            user=priced(user_replies([True]), "user-model"),
        )
        return await run_evals(cases=str(tmp_path), agents=[reads(1)], live=live)

    report = asyncio.run(body())
    assert (report.cases[0].status, report.cases[0].reason) == ("skipped", reason)


def test_an_effect_with_unrecorded_arguments_ends_the_case(tmp_path: Path) -> None:
    async def body() -> EvalReport:
        # The saved turn refunds order 42; the second simulated turn tries order 99.
        await saved_turns(
            tmp_path,
            "new-effect",
            support(REFUND_TURN),
            ["Please refund order 42."],
            must={"type": "tool_call", "data": {"name": "issue_refund"}},
            rubric=RUBRIC,
            simulate={"kind": "script", "messages": ["Now refund order 99."]},
        )
        # Recording the case issued a real refund; the eval must not add another.
        Refunds.count = 0
        now = support(
            [*REFUND_TURN, use("issue_refund", {"id": "99"}, "c3"), say("Refunded 99 too.")]
        )
        live = Live(priced([verdicts_reply([True])], "judge-model"), BUDGET)
        return await run_evals(cases=str(tmp_path), agents=[now], live=live)

    report = asyncio.run(body())
    case = report.cases[0]
    assert (case.status, case.reason) == ("error", "unmatched_external_op: issue_refund")
    assert Refunds.count == 0
