"""A channel reply survives a crash between the turn's end and the host issuing its send: the next
run of the branch, or a restarted host, derives the missing
channel_send from the log and issues it through the effect path, once."""

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import JsonValue, TypeAdapter

from threads import agent, scripted_model, sqlite
from threads.agents.store import Store, open_store, scoped
from threads.host import (
    ChannelCapabilities,
    DeliveryOutcome,
    Inbound,
    RawRequest,
    RawResponse,
    Sent,
    VerifiedDelivery,
    host,
)
from threads.host.app import ticked
from threads.host.intake import ChannelIntake
from threads.host.runs import Runner
from threads.log import Event, JsonObject, ParseError, Principal, ToolCallEvent
from threads.loop.model import LookupResult, LookupUnknown
from threads.result import Err, Ok
from threads.secrets import Secret

TEAM = "T1"
USER = Principal(issuer="fake:T1", tenant=TEAM, subject="U1")
USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}
_ITEMS: TypeAdapter[list[Inbound]] = TypeAdapter(list[Inbound])


class _CrashError(Exception):
    pass


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


@dataclass
class Replies:
    """Renders a final response as one op; `crash` makes the first render die, as a host
    that stops right after the turn ended."""

    crash: bool = False
    sent: list[JsonObject] = field(default_factory=list[JsonObject])
    agent: str = "bot"
    capabilities: ChannelCapabilities = field(
        default_factory=lambda: ChannelCapabilities("none", False, False, False, False)
    )
    limits: Mapping[str, int] = field(default_factory=dict[str, int])
    secrets: Mapping[str, Secret] = field(default_factory=dict[str, Secret])

    def verify(self, raw: RawRequest) -> Ok[VerifiedDelivery] | Err[ParseError]:
        return Ok(VerifiedDelivery(TEAM, TEAM, raw.headers["delivery"]))

    def parse(self, raw: RawRequest) -> Ok[Sequence[Inbound]] | Err[ParseError]:
        return Ok(_ITEMS.validate_json(raw.body))

    def ack(self, raw: RawRequest) -> RawResponse:
        return RawResponse(200, {}, b"")

    def render(self, event: Event) -> Sequence[JsonObject]:
        if event.type != "model_response":
            return ()
        if self.crash:
            self.crash = False
            raise _CrashError
        said = "".join(p.text for p in event.data.content if p.type == "text")
        return ({"text": said},)

    async def perform(
        self, op: JsonObject, effect_key: str, credentials: Mapping[str, str]
    ) -> DeliveryOutcome:
        self.sent.append(op)
        return Sent(f"ts{len(self.sent)}")

    async def lookup(self, effect_key: str, op: JsonObject) -> LookupResult[str]:
        return LookupUnknown("fake")


def webhook(delivery: str, key: str, words: str) -> RawRequest:
    item: JsonValue = {
        "kind": "message",
        "principal": USER.model_dump(),
        "address": "C1",
        "item_key": key,
        "content": words,
    }
    return RawRequest({"delivery": delivery}, json.dumps([item]).encode())


async def until(probe: Callable[[], Awaitable[bool]]) -> None:
    for _ in range(300):
        if await probe():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the host never got there")


async def sends(store: Store) -> list[str]:
    sq = await open_store(scoped(store, TEAM))
    rows = await sq.tables.inbox_rows()
    root = await sq.root(rows[0].thread_id)
    assert isinstance(root, Ok)
    read = await sq.read(root.value, 0)
    assert isinstance(read, Ok)
    events = read.value.fold.events
    return [e.data.call_id for e in events if isinstance(e, ToolCallEvent)]


def test_a_restarted_host_sends_the_reply_a_crash_left_unsent_once() -> None:
    store = sqlite(":memory:")
    crashing = Replies(crash=True)

    async def first() -> None:
        bot = agent(model=scripted_model({"responses": [text("Hi there.")]}))
        async with host(store=store, agents={"bot": bot}, channels={"fake": crashing}) as served:
            await served.receive("fake", webhook("d1", "m1", "hello"))
            await until(lambda: _consumed(store))
            await asyncio.sleep(0.05)
        assert crashing.sent == []

    async def restarted() -> Replies:
        channel = Replies()
        bot = agent(model=scripted_model({"responses": []}))
        async with host(store=store, agents={"bot": bot}, channels={"fake": channel}):
            await until(lambda: _sent(channel, 1))
            await asyncio.sleep(0.05)
        async with host(store=store, agents={"bot": bot}, channels={"fake": channel}) as again:
            await ticked(again)
        return channel

    asyncio.run(first())
    channel = asyncio.run(restarted())
    assert [(op["text"], op["address"]) for op in channel.sent] == [("Hi there.", "C1")]
    assert len(asyncio.run(sends(store))) == 1


def test_the_next_message_sends_the_lost_reply_before_its_own() -> None:
    store = sqlite(":memory:")
    channel = Replies(crash=True)

    async def main() -> None:
        script: JsonValue = {"responses": [text("First."), text("Second.")]}
        bot = agent(model=scripted_model(script))
        async with host(store=store, agents={"bot": bot}, channels={"fake": channel}) as served:
            await served.receive("fake", webhook("d1", "m1", "one"))
            await until(lambda: _consumed(store))
            await served.receive("fake", webhook("d2", "m2", "two"))
            await until(lambda: _sent(channel, 2))

    asyncio.run(main())
    assert [op["text"] for op in channel.sent] == ["First.", "Second."]


async def _consumed(store: Store) -> bool:
    sq = await open_store(scoped(store, TEAM))
    rows = await sq.run(
        lambda c: c.execute("SELECT count(*) FROM inbox WHERE consumed_seq IS NULL").fetchone()
    )
    return rows is not None and rows[0] == 0


async def _sent(channel: Replies, count: int) -> bool:
    return len(channel.sent) >= count


def test_stop_waits_out_an_intake_task_whose_bookkeeping_is_still_queued() -> None:
    """A consumer that finished just before stop() leaves its done-callback queued; draining
    must yield to it rather than spin on the finished task (found by the F9.1 drill)."""

    async def consumed() -> None:
        return None

    async def main() -> None:
        intake = ChannelIntake(Runner(sqlite(":memory:"), {}, {}), {})
        tasks = intake._tasks  # pyright: ignore[reportPrivateUsage] - the race needs the set
        task = asyncio.get_running_loop().create_task(consumed())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        await asyncio.sleep(0)  # the task finishes; its discard is queued behind this step
        await intake.drain()

    # A spinning drain never yields, so only a thread's join can time it out.
    stopped = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
    stopped.start()
    stopped.join(5)
    assert not stopped.is_alive(), "drain spun on a finished task"
