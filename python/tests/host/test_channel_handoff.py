"""After a handoff the conversation moves with it (spec/schema/README.md, "Channel replies"): the
target's answer is sent to the conversation, the route points at the target thread, and the next
message runs there with the target agent."""

import asyncio

from pydantic import JsonValue
from test_channel_approvals import TEAM, USAGE, ItemsChannel, message, text, until, webhook

from threads import agent, scripted_model, sqlite
from threads.agents.store import Store, open_store, scoped
from threads.host import host
from threads.log import HandoffEvent, ThreadId, UserInputEvent
from threads.result import Ok
from threads.store.sql import text_of


def _handoff(to: str) -> JsonValue:
    part: JsonValue = {
        "type": "tool_use",
        "call_id": "h1",
        "name": "handoff",
        "input": {"agent": to},
    }
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


async def _route(store: Store) -> ThreadId:
    sq = await open_store(scoped(store, TEAM))
    rows = await sq.run(lambda c: c.execute("SELECT thread_id FROM channel_threads").fetchall())
    return ThreadId(text_of(rows[0][0]))


async def _inputs(store: Store, thread_id: ThreadId) -> list[str]:
    sq = await open_store(scoped(store, TEAM))
    root = await sq.root(thread_id)
    assert isinstance(root, Ok)
    read = await sq.read(root.value, 0)
    assert isinstance(read, Ok)
    events = read.value.fold.events
    inputs = [e.data.text for e in events if isinstance(e, UserInputEvent)]
    return [t for t in inputs if isinstance(t, str)]


def test_the_conversation_follows_a_handoff() -> None:
    billing = agent(
        name="billing",
        model=scripted_model({"responses": [text("Refund issued."), text("Anything else?")]}),
    )
    front = agent(model=scripted_model({"responses": [_handoff("billing")]}), handoffs=[billing])
    channel = ItemsChannel()
    store = sqlite(":memory:")

    async def main() -> tuple[ThreadId, ThreadId]:
        async with host(store=store, agents={"bot": front}, channels={"fake": channel}) as served:
            await served.receive("fake", webhook("d1", message("m1", "refund please")))
            await until(lambda: _sent(channel, 1))
            source = await _handed_from(store)
            target = await _route(store)
            await served.receive("fake", webhook("d2", message("m2", "thanks")))
            await until(lambda: _sent(channel, 2))
            return source, target

    source, target = asyncio.run(main())
    assert source != target
    assert asyncio.run(_inputs(store, source)) == ["refund please"]
    assert asyncio.run(_inputs(store, target)) == ["refund please", "thanks"]
    assert [(op["text"], op["address"]) for op in channel.sent] == [("reply", "C1")] * 2


async def _sent(channel: ItemsChannel, count: int) -> bool | None:
    return True if len(channel.sent) >= count else None


async def _handed_from(store: Store) -> ThreadId:
    sq = await open_store(scoped(store, TEAM))
    rows = await sq.run(lambda c: c.execute("SELECT thread_id FROM inbox").fetchall())
    source = ThreadId(text_of(rows[0][0]))
    root = await sq.root(source)
    assert isinstance(root, Ok)
    read = await sq.read(root.value, 0)
    assert isinstance(read, Ok)
    assert any(isinstance(e, HandoffEvent) for e in read.value.fold.events)
    return source
