"""bar_child's answer is what the stop loop trusts (spec/schema/README.md, "Subagent cancellation
and parking"): a child that has finished (its turn closed, nothing of its own still running) gets
no stray cancel, and counts as stopped."""

import asyncio

from pydantic import JsonValue

from threads import Completed, agent, scripted_model, sqlite
from threads.log import CancelRequestedEvent, Principal
from threads.result import Ok
from threads.thread.tree import bar_child

OPERATOR = Principal(issuer="api", tenant="local", subject="operator")
ANSWER: JsonValue = {
    "content": [{"type": "text", "text": "Done."}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


def test_a_finished_child_gets_no_stray_cancel() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        child = agent(name="scanner", model=scripted_model({"responses": [ANSWER]}))
        done = await child.run("Scan.", store=store, deps=None)
        assert isinstance(done, Completed)
        assert await bar_child(store, done.thread.id, OPERATOR)
        timeline = await done.thread.timeline()
        assert isinstance(timeline, Ok)
        events = [e.event for e in timeline.value.entries]
        assert not any(isinstance(e, CancelRequestedEvent) for e in events)

    asyncio.run(main())
