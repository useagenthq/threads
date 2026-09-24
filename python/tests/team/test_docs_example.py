"""The Teams guide's complete example (docs/content/docs/(guides)/multi-agent/teams.mdx), as
written there, so the page's program stays runnable. Its last line, `asyncio.run(main())`, runs
in the test. Mirrors TypeScript's test/team/docs-example.test.ts."""

import asyncio
import re

import pytest
from pydantic import JsonValue

from threads import Completed, agent, scripted_model, sqlite

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}


def say(text: str) -> JsonValue:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": USAGE}


def start(agent_name: str, task: str) -> JsonValue:
    call: JsonValue = {"agent": agent_name, "task": task}
    return {
        "content": [{"type": "tool_use", "call_id": "c1", "name": "start", "input": call}],
        "stop_reason": "tool_use",
        "usage": USAGE,
    }


researcher = agent(
    name="researcher",
    instructions="Research the topic you are given. Answer in one sentence.",
    model=scripted_model({"responses": [say("Battery pack prices fell this year.")]}),
)

# The page's own layout, kept byte for byte.
# fmt: off
lead = agent(
    name="lead",
    instructions="Start a researcher on the topic, then report what it found.",
    model=scripted_model({
        "responses": [
            start("researcher", "Battery prices this year."),
            say("I started a researcher."),
            say("The researcher reports that battery pack prices fell this year."),
        ]
    }),
    team=[researcher],
)
# fmt: on


async def main() -> None:
    r = await lead.run("Report on battery prices.", store=sqlite(":memory:"))
    if isinstance(r, Completed):
        print(r.output)
    # The team comes with every result, whatever the status.
    print(f"team {r.team.ref.id}")


def test_the_teams_guide_example_runs_and_prints_the_leads_last_answer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(main())
    printed = capsys.readouterr().out.splitlines()
    assert printed[0] == "The researcher reports that battery pack prices fell this year."
    assert re.fullmatch(r"team [0-9a-f-]{36}", printed[1])
