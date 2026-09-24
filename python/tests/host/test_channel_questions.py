"""ask_user over a channel (spec/schema/README.md, "Questions and remembered rules"): the
question is posted once with its choices, the asker's reply answers it strictly, a reply that is
no option gets one correction and leaves it open, and another member's message waits as ordinary
input without blocking the asker. A question past its expiry is closed by the host."""

import asyncio

import pytest
from pydantic import JsonValue
from test_channel_approvals import TEAM, ItemsChannel, text, until, webhook

from threads import agent, scripted_model, sqlite
from threads.agents import run as run_module
from threads.agents.store import Store, open_store, scoped
from threads.host import expiry as expiry_module
from threads.host import host
from threads.host.app import recovered
from threads.log import AnswerRejectedEvent, Event, Principal, ToolResultEvent, TurnCompletedEvent
from threads.result import Ok

ASKER = Principal(issuer="fake:T1", tenant=TEAM, subject="U1")
OTHER = Principal(issuer="fake:T1", tenant=TEAM, subject="U2")
USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}
COLOR: JsonValue = {"question": "Which color?", "options": ["red", "blue"]}
QUESTION = "Which color?\n\n1. red\n2. blue\n\nReply with the number or the text of your choice."
CORRECTION = (
    "Please answer with one of:\n\n1. red\n2. blue\n\n"
    "Reply with the number or the text of your choice."
)
DAY = 86_400_000


def _ask(call_id: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": "ask_user", "input": COLOR}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def _says(key: str, who: Principal, words: str) -> JsonValue:
    return {
        "kind": "message",
        "principal": who.model_dump(),
        "address": "C1",
        "item_key": key,
        "content": words,
    }


async def _events(store: Store) -> list[Event]:
    sq = await open_store(scoped(store, TEAM))
    rows = await sq.tables.inbox_rows()
    root = await sq.root(rows[0].thread_id)
    assert isinstance(root, Ok)
    read = await sq.read(root.value, 0)
    assert isinstance(read, Ok)
    return list(read.value.fold.events)


async def _sent(channel: ItemsChannel, count: int) -> bool | None:
    return True if len(channel.sent) >= count else None


def test_the_asker_answers_strictly_and_another_members_message_waits() -> None:
    script: JsonValue = {"responses": [_ask("q1"), text("Done."), text("Hi.")]}
    bot = agent(model=scripted_model(script))
    channel = ItemsChannel()
    store = sqlite(":memory:")

    async def main() -> None:
        async with host(store=store, agents={"bot": bot}, channels={"fake": channel}) as served:
            await served.receive("fake", webhook("d1", _says("m1", ASKER, "deploy")))
            await until(lambda: _sent(channel, 1))
            await served.receive("fake", webhook("d2", _says("m2", OTHER, "hello")))
            await served.receive("fake", webhook("d3", _says("m3", ASKER, "green")))
            await until(lambda: _sent(channel, 2))
            # A redelivered webhook is the same inbox item: no second correction.
            await served.receive("fake", webhook("d3", _says("m3", ASKER, "green")))
            await served.receive("fake", webhook("d4", _says("m4", ASKER, "2")))
            await until(lambda: _sent(channel, 4))
            await asyncio.sleep(0.05)

    asyncio.run(main())
    texts = [op["text"] for op in channel.sent]
    assert texts == [QUESTION, CORRECTION, "reply", "reply"]
    events = asyncio.run(_events(store))
    assert len([e for e in events if isinstance(e, AnswerRejectedEvent)]) == 1
    answers = [e for e in events if isinstance(e, ToolResultEvent) and e.data.origin == "answered"]
    assert [a.data.preview for a in answers] == ["blue"]
    # The other member's message became the next turn's input, after the answer.
    done = [e for e in events if isinstance(e, TurnCompletedEvent)]
    assert [d.data.reason for d in done] == ["end_turn", "end_turn"]


def test_a_question_past_its_expiry_is_closed_by_the_host_after_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script: JsonValue = {"responses": [_ask("q1"), text("Done.")]}
    bot = agent(model=scripted_model(script))
    channel = ItemsChannel()
    store = sqlite(":memory:")

    async def main() -> None:
        async with host(store=store, agents={"bot": bot}, channels={"fake": channel}) as served:
            await served.receive("fake", webhook("d1", _says("m1", ASKER, "deploy")))
            await until(lambda: _sent(channel, 1))
        late = run_module.now_ms() + DAY + 1
        monkeypatch.setattr(run_module, "now_ms", lambda: late)
        monkeypatch.setattr(expiry_module, "now_ms", lambda: late)
        async with host(store=store, agents={"bot": bot}, channels={"fake": channel}) as served:
            await recovered(served)
            await until(lambda: _sent(channel, 2))

    asyncio.run(main())
    events = asyncio.run(_events(store))
    closed = [e for e in events if isinstance(e, ToolResultEvent) and e.data.call_id == "q1"]
    assert [(r.data.origin, r.data.preview) for r in closed] == [("not_executed", "no answer")]
    assert [op["text"] for op in channel.sent] == [QUESTION, "reply"]
