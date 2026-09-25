"""The Teams guide's second example (docs/content/docs/(guides)/multi-agent/teams.mdx, "Waiting for
members"), as written there, so the page's program stays runnable. Its last line,
`asyncio.run(main())`, runs in the test. Mirrors TypeScript's test/team/docs-wait.test.ts."""

import asyncio

import pytest
from pydantic import JsonValue

from threads import Completed, agent, scripted_model, sqlite

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}


def say(text: str) -> JsonValue:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": USAGE}


def call(call_id: str, name: str, args: JsonValue) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


# The page's own layout, kept byte for byte.
# fmt: off
researcher = agent(
    name="researcher",
    instructions="Research the topic you are given. Answer in one sentence.",
    model=scripted_model({
        "responses": [
            say("Battery pack prices fell this year."),
            say("Solar module prices fell too."),
        ]
    }),
)

lead = agent(
    name="lead",
    instructions="Start a researcher per topic, wait for both, then summarize.",
    model=scripted_model({
        "responses": [
            call("c1", "start", {"agent": "researcher", "task": "Battery prices."}),
            call("c2", "start", {"agent": "researcher", "task": "Solar prices."}),
            call("c3", "wait", {"members": ["researcher-1", "researcher-2"]}),
            say("Battery and solar prices both fell this year."),
            say("Their reports are in: battery and solar prices both fell."),
        ]
    }),
    team=[researcher],
)
# fmt: on


async def main() -> None:
    r = await lead.run("Compare battery and solar prices.", store=sqlite(":memory:"))
    if isinstance(r, Completed):
        print(r.output)


def test_the_teams_guide_wait_example_runs_and_prints_the_leads_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(main())
    printed = capsys.readouterr().out.splitlines()
    assert printed == ["Their reports are in: battery and solar prices both fell."]
