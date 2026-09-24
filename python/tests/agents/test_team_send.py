"""send_message reaches only the team (spec/schema/README.md, "Team tools"): a name that is
neither `*` nor a member is refused as an error result that lists the team, and nothing else is
appended."""

import asyncio

from pydantic import JsonValue

from threads import Completed, Thread, agent, scripted_model, sqlite
from threads.log import Event, TeamMessageEvent, ToolResultEvent
from threads.result import Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def send(to: str, call_id: str) -> JsonValue:
    part: JsonValue = {
        "type": "tool_use",
        "call_id": call_id,
        "name": "send_message",
        "input": {"to": to, "text": "Refund order 42."},
    }
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


async def events_of(thread: Thread) -> list[Event]:
    timeline = await thread.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


def test_an_unknown_recipient_is_refused_and_a_member_is_not() -> None:
    async def main() -> None:
        reviewer = agent(name="reviewer", model=scripted_model({"responses": []}))
        lead = agent(
            name="lead",
            model=scripted_model(
                {"responses": [send("billing", "c1"), send("reviewer", "c2"), text("Done.")]}
            ),
            subagents=[reviewer],
        )
        result = await lead.run("Tell them.", store=sqlite(":memory:"))
        assert isinstance(result, Completed)
        events = await events_of(result.thread)
        results = [
            (e.data.is_error, e.data.preview) for e in events if isinstance(e, ToolResultEvent)
        ]
        assert results == [
            (
                True,
                "unknown_recipient: billing is not on this team; send to lead, reviewer or * "
                "for everyone",
            ),
            (False, "sent"),
        ]
        sent = [e.data.to for e in events if isinstance(e, TeamMessageEvent)]
        assert sent == ["reviewer"]

    asyncio.run(main())


def test_a_member_messages_a_named_sibling() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        writer = agent(name="writer", model=scripted_model({"responses": []}))
        reviewer = agent(
            name="reviewer",
            model=scripted_model({"responses": [send("writer", "r1"), text("Told the writer.")]}),
        )
        spawn: JsonValue = {
            "content": [
                {
                    "type": "tool_use",
                    "call_id": "c1",
                    "name": "spawn_agent",
                    "input": {"agent": "reviewer", "prompt": "Review, then tell the writer."},
                }
            ],
            "stop_reason": "tool_use",
            "usage": USAGE,
        }
        lead = agent(
            name="lead",
            model=scripted_model({"responses": [spawn, text("Done.")]}),
            subagents=[reviewer, writer],
        )
        result = await lead.run("Review.", store=store)
        assert isinstance(result, Completed)
        events = await events_of(result.thread)
        sent = [(e.data.from_, e.data.to) for e in events if isinstance(e, TeamMessageEvent)]
        assert sent == [("reviewer", "writer")]

    asyncio.run(main())
