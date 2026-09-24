"""Invariant 3 for the host's own question and correction sends (review of lane 14A): a send left
in doubt by a crash is the host's to settle, never the agent's. The loop neither parks the turn
behind it nor re-authorizes it as a model call; outbound reconciles it after its question settled,
and a send that never began and is no longer due is closed without sending."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest
from host.test_channel_approvals import TEAM, ItemsChannel, text, webhook
from host.test_channel_questions import ASKER, CORRECTION, QUESTION
from pydantic import JsonValue

from threads import agent, scripted_model, sqlite
from threads.agents.store import Store, open_store, scoped
from threads.host import ChannelCapabilities, DeliveryOutcome, Sent, host
from threads.host import deliver as deliver_module
from threads.host.app import recovered
from threads.log import (
    ApprovalRequestedEvent,
    CallId,
    Event,
    JsonObject,
    ToolResultEvent,
    TurnCompletedEvent,
    UserInputEvent,
)
from threads.loop.model import Found, LookupResult, NotFound
from threads.reduce.run_end import run_end
from threads.result import Ok
from threads.thread.handle import open_thread

STOP_S = 3.0
USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}
COLOR: JsonValue = {"question": "Which color?", "options": ["red", "blue"]}


def _ask() -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": "q1", "name": "ask_user", "input": COLOR}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def _says(key: str, words: str) -> JsonValue:
    who: JsonValue = ASKER.model_dump()
    return {"kind": "message", "principal": who, "address": "C1", "item_key": key, "content": words}


@dataclass
class Crashy(ItemsChannel):
    """What the provider really posted. A send whose text starts with `hang` lands, then the host
    dies before it hears back: the send is begun and never settled."""

    hang: str | None = None
    landed: list[str] = field(default_factory=list[str])
    sending: asyncio.Event = field(default_factory=asyncio.Event)

    async def perform(
        self, op: JsonObject, effect_key: str, credentials: Mapping[str, str]
    ) -> DeliveryOutcome:
        self.landed.append(effect_key)
        self.sent.append(op)
        words = op.get("text")
        if self.hang is not None and isinstance(words, str) and words.startswith(self.hang):
            self.hang = None
            self.sending.set()
            await asyncio.Event().wait()
        return Sent(f"ts{len(self.sent)}")

    async def lookup(self, effect_key: str, op: JsonObject) -> LookupResult[str]:
        return Found("ts-found") if effect_key in self.landed else NotFound()


def _caps(lookup: str) -> ChannelCapabilities:
    return ChannelCapabilities(lookup, True, False, False, False)  # pyright: ignore[reportArgumentType] - "final" | "none"


async def _events(store: Store) -> list[Event]:
    sq = await open_store(scoped(store, TEAM))
    rows = await sq.tables.inbox_rows()
    root = await sq.root(rows[0].thread_id)
    assert isinstance(root, Ok)
    read = await sq.read(root.value, 0)
    assert isinstance(read, Ok)
    return list(read.value.fold.events)


async def _pause() -> None:
    await asyncio.sleep(0.3)


def _texts(channel: Crashy, prefix: str) -> list[object]:
    return [op["text"] for op in channel.sent if str(op["text"]).startswith(prefix)]


def _completed(events: list[Event]) -> bool:
    return any(isinstance(e, TurnCompletedEvent) and e.data.reason == "end_turn" for e in events)


def _answer(events: list[Event]) -> list[str]:
    return [
        e.data.preview
        for e in events
        if isinstance(e, ToolResultEvent) and e.data.origin == "answered"
    ]


def _crash_then_answer(lookup: str, lead: bool = False) -> tuple[Crashy, list[Event]]:
    model = scripted_model({"responses": [_ask(), text("Done.")]})
    # A team lead waits on its members; its own question send is never one of them.
    bot = agent(model=model, team=[]) if lead else agent(model=model)
    store = sqlite(":memory:")
    channel = Crashy(hang="Which color?", capabilities=_caps(lookup))

    async def main() -> None:
        first = host(store=store, agents={"bot": bot}, channels={"fake": channel})
        await first.ready()
        await first.receive("fake", webhook("d1", _says("m1", "deploy")))
        await asyncio.wait_for(channel.sending.wait(), STOP_S)
        await asyncio.wait_for(first.stop(), STOP_S)
        async with host(store=store, agents={"bot": bot}, channels={"fake": channel}) as again:
            await recovered(again)
            await _pause()
            await again.receive("fake", webhook("d9", _says("m9", "2")))
            await _pause()

    asyncio.run(main())
    return channel, asyncio.run(_events(store))


@pytest.mark.parametrize("lead", [False, True])
@pytest.mark.parametrize("lookup", ["final", "none"])
def test_a_question_send_in_doubt_never_holds_the_answered_turn(lookup: str, lead: bool) -> None:
    channel, events = _crash_then_answer(lookup, lead)
    assert _texts(channel, "Which color?") == [QUESTION]
    assert _answer(events) == ["blue"]
    assert _completed(events)
    request = next(e for e in events if isinstance(e, UserInputEvent))
    # With no lookup the send stays parked for a human, and the run is still completed.
    assert run_end(events, request.event_id).status == "completed"


def test_a_question_send_that_never_began_is_closed_once_its_question_is_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = deliver_module.calls.run_call
    entered = asyncio.Event()
    armed = [True]

    async def dying(rt: object, call_id: CallId) -> object:
        if armed[0] and call_id.startswith("question_"):
            armed[0] = False
            entered.set()
            await asyncio.Event().wait()
        return await real(rt, call_id)  # pyright: ignore[reportArgumentType] - the probe's shim

    monkeypatch.setattr(deliver_module.calls, "run_call", dying)
    bot = agent(model=scripted_model({"responses": [_ask(), text("Done.")]}))
    store = sqlite(":memory:")
    channel = Crashy(capabilities=_caps("final"))

    async def main() -> None:
        first = host(store=store, agents={"bot": bot}, channels={"fake": channel})
        await first.ready()
        await first.receive("fake", webhook("d1", _says("m1", "deploy")))
        await asyncio.wait_for(entered.wait(), STOP_S)
        await asyncio.wait_for(first.stop(), STOP_S)
        # Answered while no host runs (the HTTP API of another deployment, say).
        events = await _events(store)
        sq = await open_store(scoped(store, TEAM))
        rows = await sq.tables.inbox_rows()
        opened = await open_thread(scoped(store, TEAM), rows[0].thread_id)
        assert isinstance(opened, Ok), events
        answered = await opened.value.answer(CallId("q1"), "2", ASKER)
        assert isinstance(answered, Ok), answered
        async with host(store=store, agents={"bot": bot}, channels={"fake": channel}) as again:
            await recovered(again)
            await _pause()

    asyncio.run(main())
    events = asyncio.run(_events(store))
    assert _texts(channel, "Which color?") == []
    closed = [
        e.data
        for e in events
        if isinstance(e, ToolResultEvent) and e.data.call_id.startswith("question_")
    ]
    assert [(r.origin, r.is_error, r.preview) for r in closed] == [
        ("not_executed", True, "not sent: no longer due")
    ]
    assert not any(isinstance(e, ApprovalRequestedEvent) for e in events)
    assert _completed(events)


def test_a_correction_send_in_doubt_is_reconciled_not_sent_twice() -> None:
    bot = agent(model=scripted_model({"responses": [_ask(), text("Done.")]}))
    store = sqlite(":memory:")
    channel = Crashy(capabilities=_caps("final"))

    async def main() -> None:
        first = host(store=store, agents={"bot": bot}, channels={"fake": channel})
        await first.ready()
        await first.receive("fake", webhook("d1", _says("m1", "deploy")))
        for _ in range(300):
            if channel.sent:
                break
            await asyncio.sleep(0.01)
        channel.hang = "Please answer"
        await first.receive("fake", webhook("d2", _says("m2", "green")))
        await asyncio.wait_for(channel.sending.wait(), STOP_S)
        await asyncio.wait_for(first.stop(), STOP_S)
        async with host(store=store, agents={"bot": bot}, channels={"fake": channel}) as again:
            await recovered(again)
            await _pause()
            await again.receive("fake", webhook("d9", _says("m9", "2")))
            await _pause()

    asyncio.run(main())
    events = asyncio.run(_events(store))
    assert _texts(channel, "Please answer") == [CORRECTION]
    assert _completed(events)
