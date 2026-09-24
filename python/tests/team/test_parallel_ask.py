"""Team calls beside other calls in one response (design §4.8 and §4.12): an opened ask or wait
parks its turn only once nothing else in the turn is runnable, with one park per ask or wait. The
other calls run first, and each answer resumes its own park. Reviewer probes H1 (21E.1). Mirrors
TypeScript's test/team/parallel-ask.test.ts."""

import asyncio
from collections.abc import Sequence

from pydantic import JsonValue, TypeAdapter
from team.run_kit import (
    USAGE,
    answers,
    events,
    reply_to,
    result_of,
    say,
    sq_of,
    start,
    types,
)
from team.team_kit import assert_team_replays

from threads import Agent, AskResult, Completed, WaitResult, agent, scripted_model, sqlite
from threads.log import Event, ToolResultEvent

_ASK = TypeAdapter[AskResult](AskResult)


def _calls(*made: tuple[str, str, JsonValue]) -> JsonValue:
    """One response making several tool calls."""
    parts: list[JsonValue] = [
        {"type": "tool_use", "call_id": i, "name": name, "input": args} for i, name, args in made
    ]
    return {"content": parts, "stop_reason": "tool_use", "usage": USAGE}


def _replier(name: str) -> Agent[None, str]:
    """A member that reads its task, then replies to the ask it is shown."""
    return agent(
        name=name,
        model=answers(
            [
                lambda _r: say("Read."),
                lambda r: reply_to("r1", r, f"{name} answer."),
                lambda _r: say("Replied."),
            ]
        ),
    )


def _done(name: str, *texts: str) -> Agent[None, str]:
    return agent(name=name, model=scripted_model({"responses": [say(t) for t in texts]}))


def _run_lead(response: JsonValue, team: Sequence[Agent[None, str]]) -> Sequence[Event]:
    async def main() -> Sequence[Event]:
        store = sqlite(":memory:")
        script = [start("c1", "a", "Go."), start("c2", "b", "Go."), response]
        script += [say("Final.")] * 6
        lead = agent(name="lead", model=scripted_model({"responses": script}), team=list(team))
        r = await asyncio.wait_for(lead.run("Go.", store=store), 20)
        assert isinstance(r, Completed)
        log = await events(store, r.thread)
        await assert_team_replays(await sq_of(store), r.team.ref.id)
        return log

    return asyncio.run(main())


def test_two_asks_in_one_response_both_park_and_each_reply_closes_its_own() -> None:
    log = _run_lead(
        _calls(
            ("q1", "ask", {"to": "a-1", "question": "A?"}),
            ("q2", "ask", {"to": "b-1", "question": "B?"}),
        ),
        [_replier("a"), _replier("b")],
    )
    assert result_of(log, "q1", _ASK)["text"] == "a answer."
    assert result_of(log, "q2", _ASK)["text"] == "b answer."
    assert types(log).count("parked") == types(log).count("resumed") == len(("q1", "q2"))


def test_an_ask_and_a_wait_in_one_response_both_close() -> None:
    log = _run_lead(
        _calls(
            ("q1", "ask", {"to": "a-1", "question": "A?"}),
            ("q2", "wait", {"members": ["b-1"]}),
        ),
        [_replier("a"), _done("b", "b done.")],
    )
    assert result_of(log, "q1", _ASK)["status"] == "answered"
    waited = result_of(log, "q2", TypeAdapter[WaitResult](WaitResult))
    assert (waited["status"], waited["timed_out"]) == ("waited", False)


def test_an_ask_then_a_send_the_send_runs_then_the_ask_parks_and_closes() -> None:
    log = _run_lead(
        _calls(
            ("q1", "ask", {"to": "a-1", "question": "A?"}),
            ("q2", "send", {"to": "b-1", "text": "FYI."}),
        ),
        [_replier("a"), _done("b", "b done.", "Noted.")],
    )
    assert result_of(log, "q1")["status"] == "answered"
    assert result_of(log, "q2")["status"] == "sent"
    results = [str(e.data.call_id) for e in log if isinstance(e, ToolResultEvent)]
    assert results.index("q2") < results.index("q1")
    assert types(log).count("parked") == 1
