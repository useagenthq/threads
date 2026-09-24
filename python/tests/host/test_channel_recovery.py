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
from threads.agents.agent import Agent
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
from threads.host.app import recovered
from threads.host.intake import ChannelIntake
from threads.host.runs import Runner
from threads.log import Event, JsonObject, ParseError, Principal, ToolCallEvent, TurnCompletedEvent
from threads.loop.model import LookupResult, LookupUnknown
from threads.result import Err, Ok
from threads.secrets import Secret

TEAM = "T1"
USER = Principal(issuer="fake:T1", tenant=TEAM, subject="U1")
USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}
_ITEMS: TypeAdapter[list[Inbound]] = TypeAdapter(list[Inbound])
CONTROL_AND_FOLLOW_ON = 2
"""Resumes for a control answered mid-run: the control's own, then the follow-on it queued."""


class _CrashError(Exception):
    pass


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


@dataclass
class Replies:
    """Renders a final response as one op; `crash` makes the first render die, as a host
    that stops right after the turn ended. `hold` keeps each send waiting until it is set;
    `sending` is set once a send has started."""

    crash: bool = False
    hold: asyncio.Event | None = None
    sending: asyncio.Event = field(default_factory=asyncio.Event)
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

    def render_text(self, text: str) -> Sequence[JsonObject]:
        return ({"text": text},)

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
        self.sending.set()
        if self.hold is not None:
            await self.hold.wait()
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


class _Dead(Replies):
    """A channel whose every render dies: the host that has it never sends a reply, whichever of
    its paths (the run's delivery, its own recovery pass) gets to the reply first."""

    def render_text(self, text: str) -> Sequence[JsonObject]:
        return ({"text": text},)

    def render(self, event: Event) -> Sequence[JsonObject]:
        if event.type == "model_response":
            raise _CrashError
        return ()


async def crashed(store: Store, bot: Agent[None, object] | None = None) -> None:
    """A host that died right after its turn ended: the reply is in the log, never sent.
    `bot` answers "Hi there." first; a test that runs its own config passes it. The host stops
    once the turn has ended `end_turn`, the moment a reply is owed. Stopped any earlier, the turn
    ends `interrupted` and no reply is owed; a fixed sleep here raced that on Linux."""
    dead = _Dead()
    bot = bot or agent(model=scripted_model({"responses": [text("Hi there.")]}))
    async with host(store=store, agents={"bot": bot}, channels={"fake": dead}) as served:
        await served.receive("fake", webhook("d1", "m1", "hello"))
        await until(lambda: _turn_ended(store))
    assert dead.sent == []


async def _turn_ended(store: Store) -> bool:
    sq = await open_store(scoped(store, TEAM))
    rows = await sq.tables.inbox_rows()
    root = None if not rows else await sq.root(rows[0].thread_id)
    read = None if not isinstance(root, Ok) else await sq.read(root.value, 0)
    events = read.value.fold.events if isinstance(read, Ok) else ()
    return any(isinstance(e, TurnCompletedEvent) and e.data.reason == "end_turn" for e in events)


def test_a_restarted_host_sends_the_reply_a_crash_left_unsent_once() -> None:
    store = sqlite(":memory:")

    async def restarted() -> Replies:
        hold = asyncio.Event()
        channel = Replies(hold=hold)
        bot = agent(model=scripted_model({"responses": []}))
        async with host(store=store, agents={"bot": bot}, channels={"fake": channel}) as served:
            done = asyncio.ensure_future(recovered(served))
            await channel.sending.wait()
            # Recovery is mid-send: the seam has not resolved yet.
            assert not done.done()
            hold.set()
            await done
            # The pass the seam observed includes the redelivery.
            assert [op["text"] for op in channel.sent] == ["Hi there."]
        channel.hold = None
        async with host(store=store, agents={"bot": bot}, channels={"fake": channel}) as again:
            await recovered(again)
        return channel

    asyncio.run(crashed(store))
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


def test_the_same_host_started_again_waits_for_its_new_recovery_pass() -> None:
    store = sqlite(":memory:")

    async def main() -> None:
        hold = asyncio.Event()
        channel = Replies(hold=hold)
        bot = agent(model=scripted_model({"responses": []}))
        served = host(store=store, agents={"bot": bot}, channels={"fake": channel})
        await served.ready()
        await recovered(served)  # nothing to recover yet
        await served.stop()
        await crashed(store)
        await served.ready()
        done = asyncio.ensure_future(recovered(served))
        await channel.sending.wait()
        # The first start's pass finished long ago; the seam waits for this one.
        assert not done.done()
        hold.set()
        await done
        assert [op["text"] for op in channel.sent] == ["Hi there."]
        await served.stop()

    asyncio.run(main())


def test_settled_waits_for_a_follow_on_resume_a_recovered_run_scheduled() -> None:
    store = sqlite(":memory:")

    async def main() -> None:
        await crashed(store)
        hold = asyncio.Event()
        channel = Replies(hold=hold)
        bot = agent(model=scripted_model({"responses": []}))
        runner = Runner(store, {"bot": bot}, {"fake": channel})
        runner.resolve_secrets()
        tenant = runner.store(TEAM)
        sq = await open_store(tenant)
        thread_id = (await sq.tables.inbox_rows())[0].thread_id
        root = await sq.root(thread_id)
        assert isinstance(root, Ok)
        await runner.redeliver(tenant, thread_id)
        await channel.sending.wait()
        resumed: list[object] = []
        resume = runner.resume

        async def counted(*args: object, **kwargs: object) -> object:
            resumed.append(args)
            return await resume(*args, **kwargs)  # pyright: ignore[reportArgumentType] - a spy

        runner.resume = counted  # pyright: ignore[reportAttributeAccessIssue] - a spy
        # A control while the recovered run is in flight: its resume follows once that run ends.
        await runner.resume(tenant, thread_id, root.value)
        hold.set()
        await runner.settled()
        # The control's resume and the follow-on it queued have both run; none is left.
        assert len(resumed) == CONTROL_AND_FOLLOW_ON
        assert not runner._pending  # pyright: ignore[reportPrivateUsage] - what settled covers
        assert not runner.running(root.value)
        await runner.stop()

    asyncio.run(main())
