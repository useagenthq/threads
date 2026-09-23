"""A run started through the run API resumes when the host that ran it stalls or dies: the next
host's recovery pass runs its open turn from the log with no new input. The "crash" is a host
whose model or tool never answers and is never stopped; its lease is expired, as the drills do,
and a new host starts on the same store."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from pydantic import BaseModel, JsonValue

from threads import RunContext, Store, agent, scripted_model, sqlite, tool
from threads._generated.host_api_v1 import RunAccepted, StartRunRequest
from threads.agents.store import open_store, scoped
from threads.host import Host, host
from threads.host.app import recovered
from threads.log import (
    EffectBeginEvent,
    Event,
    ModelRequestEvent,
    ModelResponseEvent,
    Permissions,
    Principal,
    TurnCompletedEvent,
    UserInputEvent,
)
from threads.loop.model import (
    ModelChunk,
    ModelContext,
    ModelRequest,
)
from threads.loop.scripted import ScriptedModel
from threads.reduce import Fold
from threads.result import Ok

ALICE = Principal(issuer="api", tenant="acme", subject="alice")
EVE = Principal(issuer="api", tenant="other", subject="eve")
USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}
ALLOW_CHARGE = Permissions(
    mode="default",
    allow=["charge"],
    ask=[],
    deny=[],
    protected_paths=[".git/**"],
    allow_bypass=False,
    plan_exit_mode="default",
)


class Empty(BaseModel):
    pass


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use(name: str, call_id: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": {}}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


class Counted(ScriptedModel):
    """A scripted model that counts its requests and answers none until `held` is set: the host
    that holds one is as good as dead."""

    def __init__(self, *replies: JsonValue) -> None:
        super().__init__(scripted_model({"responses": list(replies)})._entries, {})
        self.held = asyncio.Event()
        self.calls = 0

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        self.calls += 1
        await self.held.wait()
        async for chunk in super().send(request, context):
            yield chunk


def stalled(*replies: JsonValue) -> Counted:
    return Counted(*replies)


def answering(*replies: JsonValue) -> Counted:
    model = stalled(*replies)
    model.held.set()
    return model


def support(store: Store, model: Counted) -> Host:
    return host(store=store, agents={"support": agent(model=model)})


async def start(served: Host, who: Principal) -> RunAccepted:
    request = StartRunRequest.model_validate({"agent": "support", "input": "Hello"})
    started = await served.start_run(request, principal=who, idempotency_key="k-1")
    assert isinstance(started, Ok), started
    return started.value


async def fold(store: Store, who: Principal, run: RunAccepted) -> Fold:
    read = await (await open_store(scoped(store, who.tenant))).read(run.branch_id, 0)
    assert isinstance(read, Ok), read
    return read.value.fold


async def until(probe: Callable[[], Awaitable[bool]]) -> None:
    for _ in range(500):
        if await probe():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the host never got there")


def has(
    store: Store, who: Principal, run: RunAccepted, kind: type[Event]
) -> Callable[[], Awaitable[bool]]:
    async def probe() -> bool:
        return any(isinstance(e, kind) for e in (await fold(store, who, run)).events)

    return probe


async def expire_leases(store: Store) -> None:
    """The stalled host's lease runs out (its TTL is 30 s): the drills do the same."""
    sq = await open_store(store)
    await sq.run(lambda c: c.execute("UPDATE leases SET expires_at = 0"))


def texts(events: Sequence[Event]) -> list[str]:
    return [
        p.text
        for e in events
        if isinstance(e, ModelResponseEvent)
        for p in e.data.content
        if p.type == "text"
    ]


def ids(events: Sequence[Event]) -> list[str]:
    return [e.event_id for e in events]


async def status(served: Host, who: Principal, run: RunAccepted, after_seq: int) -> JsonValue:
    """The run's stream from `after_seq`, to its result's status."""
    stream = await served.subscribe(run.thread_id, run.run_id, principal=who, after_seq=after_seq)
    assert isinstance(stream, Ok), stream
    last: JsonValue = None
    async for message in stream.value:
        assert message.id is None or message.id > after_seq
        last = message.data
    assert isinstance(last, dict)
    assert last["run_id"] == run.run_id
    assert isinstance(last["result"], dict)
    return last["result"]["status"]


def test_an_open_turn_completes_without_new_input_in_its_own_tenant() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        late = stalled(text("late"), text("late"))
        first = support(store, late)
        runs = [(ALICE, await start(first, ALICE)), (EVE, await start(first, EVE))]
        for who, run in runs:
            await until(has(store, who, run, ModelRequestEvent))
        alice_run = runs[0][1]
        seen = (await fold(store, ALICE, alice_run)).seq
        await expire_leases(store)

        model = answering(text("done"), text("done"))
        async with support(store, model) as second:
            await recovered(second)
            for who, run in runs:
                events = (await fold(store, who, run)).events
                assert isinstance(events[-1], TurnCompletedEvent)
                inputs = [e for e in events if isinstance(e, UserInputEvent)]
                assert [e.event_id for e in inputs] == [run.run_id]
                assert texts(events) == ["done"]
            assert model.calls == len(runs)
            assert await status(second, ALICE, alice_run, seen) == "completed"

        # The stalled host wakes with its answer: the fence refuses it.
        settled = ids((await fold(store, ALICE, alice_run)).events)
        late.held.set()
        await first.stop()
        assert ids((await fold(store, ALICE, alice_run)).events) == settled

    asyncio.run(main())


def test_a_parked_run_stays_parked() -> None:
    sent: list[str] = []

    async def send(_args: Empty, _ctx: RunContext[None]) -> str:
        sent.append("x")
        return "sent"

    def bot(store: Store) -> Host:
        mail = tool(name="send_email", description="Send.", input=Empty, runs="host", execute=send)
        model = scripted_model({"responses": [use("send_email", "m1")]})
        return host(store=store, agents={"support": agent(model=model, tools=[mail])})

    async def main() -> None:
        store = sqlite(":memory:")
        async with bot(store) as first:
            run = await start(first, ALICE)
            await until(lambda: _parked(store, run))
        parked = ids((await fold(store, ALICE, run)).events)
        async with bot(store) as second:
            await recovered(second)
        assert ids((await fold(store, ALICE, run)).events) == parked
        assert sent == []

    asyncio.run(main())


async def _parked(store: Store, run: RunAccepted) -> bool:
    return bool((await fold(store, ALICE, run)).parked)


def test_an_effect_that_began_parks_and_is_never_sent_again() -> None:
    charged: list[str] = []

    async def main() -> None:
        store = sqlite(":memory:")
        held = asyncio.Event()

        async def charge(_args: Empty, _ctx: RunContext[None]) -> str:
            charged.append("x")
            await held.wait()
            return "charged"

        def bot(model: Counted) -> Host:
            card = tool(
                name="charge", description="Charge.", input=Empty, runs="host", execute=charge
            )
            chosen = agent(model=model, tools=[card], permissions=ALLOW_CHARGE)
            return host(store=store, agents={"support": chosen})

        first = bot(answering(use("charge", "c1"), text("late")))
        run = await start(first, ALICE)
        await until(has(store, ALICE, run, EffectBeginEvent))
        await expire_leases(store)
        model = answering(text("never"))
        async with bot(model) as second:
            await recovered(second)
            parked = (await fold(store, ALICE, run)).parked
            assert [p.kind for p in parked] == ["effect"]
        assert charged == ["x"]
        assert model.calls == 0
        held.set()
        await first.stop()

    asyncio.run(main())


def test_two_hosts_starting_together_resume_it_once() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        late = stalled(text("late"))
        first = support(store, late)
        run = await start(first, ALICE)
        await until(has(store, ALICE, run, ModelRequestEvent))
        seen = (await fold(store, ALICE, run)).seq
        await expire_leases(store)

        models = [stalled(text("done")), stalled(text("done"))]
        hosts = [support(store, m) for m in models]
        await asyncio.gather(*(h.ready() for h in hosts))
        await until(lambda: _called(models))
        winner = next(i for i, m in enumerate(models) if m.calls)
        loser = hosts[1 - winner]
        await recovered(loser)
        # The loser's refused resume is no answer: its subscriber waits for the winner's run.
        waiting = asyncio.ensure_future(status(loser, ALICE, run, seen))
        await asyncio.sleep(0.1)
        models[winner].held.set()
        assert await waiting == "completed"
        await recovered(hosts[winner])
        assert sum(m.calls for m in models) == 1
        events = (await fold(store, ALICE, run)).events
        assert texts(events) == ["done"]
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
        epochs = [e.epoch for e in events]
        assert epochs == sorted(epochs)
        for h in hosts:
            await h.stop()
        late.held.set()
        await first.stop()

    asyncio.run(main())


async def _called(models: list[Counted]) -> bool:
    return any(m.calls for m in models)


def test_a_stalled_host_that_lost_the_run_never_answers_for_it() -> None:
    """Its own run is fenced when it wakes, but the run goes on elsewhere: a subscriber on the
    stalled host waits for the log instead of reporting that host's failure."""

    async def main() -> None:
        store = sqlite(":memory:")
        late = stalled(text("late"))
        first = support(store, late)
        run = await start(first, ALICE)
        await until(has(store, ALICE, run, ModelRequestEvent))
        seen = (await fold(store, ALICE, run)).seq
        await expire_leases(store)
        model = stalled(text("done"))
        async with support(store, model) as second:
            await until(lambda: _called([model]))
            late.held.set()
            await until(lambda: _ended(first, run))
            waiting = asyncio.ensure_future(status(first, ALICE, run, seen))
            await asyncio.sleep(0.1)
            model.held.set()
            assert await waiting == "completed"
            await recovered(second)
        await first.stop()

    asyncio.run(main())


async def _ended(served: Host, run: RunAccepted) -> bool:
    runner = served._runner  # pyright: ignore[reportPrivateUsage] - the stalled host's run
    return not runner.running(run.branch_id)
