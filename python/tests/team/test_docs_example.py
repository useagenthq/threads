"""The Teams guide's complete example (docs/content/docs/(guides)/multi-agent/teams.mdx), as
written there, so the page's program stays runnable. Its last line, `asyncio.run(main())`, runs
in the test. Mirrors TypeScript's test/team/docs-example.test.ts."""

import asyncio
import re

import pytest
from pydantic import JsonValue

from threads import Completed, TeamAgent, TeamCursor, agent, scripted_model, sqlite

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


def _page_lead() -> TeamAgent[None, str]:
    """The page's own lead, rebuilt: a scripted model's script is consumed by one run."""
    member = agent(
        name="researcher",
        instructions="Research the topic you are given. Answer in one sentence.",
        model=scripted_model({"responses": [say("Battery pack prices fell this year.")]}),
    )
    return agent(
        name="lead",
        instructions="Start a researcher on the topic, then report what it found.",
        model=scripted_model(
            {
                "responses": [
                    start("researcher", "Battery prices this year."),
                    say("I started a researcher."),
                    say("The researcher reports that battery pack prices fell this year."),
                ]
            }
        ),
        team=[member],
    )


async def _following() -> list[str]:
    """The guide's "Following the team's events" snippet, as written there."""
    r = await _page_lead().run("Report on battery prices.", store=sqlite(":memory:"))
    team = r.team
    printed: list[str] = []

    # A live tail. It ends when the team closes, or when you stop iterating.
    last: TeamCursor | None = None
    async for item in team.events(follow=True):
        last = item.cursor
        # The feed was rebuilt under a new epoch: read on from the cursor it names.
        if item.kind == "epoch_restarted":
            continue
        who = item.source.member.name if item.source.kind == "member" else item.source.kind
        printed.append(f"{item.cursor.epoch}:{item.cursor.offset} {who} {item.event.type}")
        if item.event.type == "member_started":
            break

    # Later, or in another process: exactly where that reader stopped.
    async for item in team.events(after=last):
        printed.append(item.event.type if item.kind == "event" else item.kind)
    return printed


def test_the_guides_follow_snippet_tails_the_feed_then_resumes_from_its_last_cursor() -> None:
    printed = asyncio.run(_following())
    assert printed[0] == "1:1 team team_opened"
    # The tail stopped at the lead's member_started; the resume read the rest, each item once.
    tailed = [line for line in printed if line.startswith(("1:", "2:"))]
    assert tailed[-1].endswith("member_started")
    assert len(printed) > len(tailed)
