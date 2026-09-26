"""The host wakes an idle thread (Gate 1 §2.7.2 and §2.7.3; design §7 Phase 2 A). Mail a member
sends after the lead's run has ended is consumed by the host's team tick, which opens a turn of
the request the mail belongs to, and the host runs that turn on: no new run is started, and on a
channel thread the woken turn's answer is delivered as a further reply. A background child a crash
stopped is woken the same way, through its pending_wakes row. The same cases as TypeScript's
host/test/team-wake.test.ts."""

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable, Sequence

import httpx
import pytest
from host.test_api_recovery import USAGE, answering, expire_leases, stalled, until
from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING
from team.run_kit import call, say, sq_of
from team.team_kit import assert_team_replays

from threads import (
    Agent,
    Model,
    Principal,
    Store,
    TeamRef,
    agent,
    open_team,
    scripted_model,
    sqlite,
)
from threads._generated.host_api_v1 import StartRunRequest
from threads.agents.store import now_ms, open_store, scoped
from threads.host import Host, RawRequest, host
from threads.log import (
    BranchId,
    Event,
    MessageReceivedEvent,
    ModelResponseEvent,
    ThreadStartedEvent,
    TurnCompletedEvent,
    UserInputEvent,
    WokenEvent,
)
from threads.result import Ok
from threads.secrets import secret
from threads.slack import slack

ALICE = Principal(issuer="api", tenant="acme", subject="alice")
TENANT = "slack:T1"
CHATTER = Principal(issuer=TENANT, tenant=TENANT, subject="U1")
SECRET = "shh-signing"  # noqa: S105 - a test signing secret


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")


def helper(name: str, to: str, text: str) -> Agent[None, str]:
    """A helper that sends one message to `to` and then reports; its turn ends idle."""
    script = [call("s1", "send", {"to": to, "text": text}), say("Reported.")]
    return agent(name=name, model=scripted_model({"responses": script}))


async def events_of(store: Store, tenant: str, branch: BranchId) -> Sequence[Event]:
    read = await (await open_store(scoped(store, tenant))).read(branch, now_ms())
    assert isinstance(read, Ok), read
    return read.value.fold.events


def turns(events: Sequence[Event]) -> int:
    return sum(1 for e in events if isinstance(e, TurnCompletedEvent))


def answers(events: Sequence[Event]) -> list[str]:
    return [
        p.text
        for e in events
        if isinstance(e, ModelResponseEvent)
        for p in e.data.content
        if p.type == "text"
    ]


async def team_of(store: Store, tenant: str, branch: BranchId) -> str:
    """The team of the lead's thread, from its pinned thread_started."""
    events = await events_of(store, tenant, branch)
    started = next(e for e in events if isinstance(e, ThreadStartedEvent))
    team = started.data.team
    assert team is not MISSING, started
    return team.id


async def operator_starts(store: Store, team: str, who: Principal, named: str) -> None:
    """The operator starts `named` on the lead's team, as another process would."""
    opened = await open_team(store, TeamRef(who.tenant, team), principal=who)
    assert isinstance(opened, Ok), opened
    started = await opened.value.start(named, "Scan the deps.")
    assert started.status == "started", started


def test_a_members_send_after_the_run_ended_wakes_the_hosted_lead() -> None:
    async def main() -> None:
        lead = agent(
            name="wake_lead",
            model=scripted_model({"responses": [say("Standing by."), say("The scan is clean.")]}),
            team=[helper("wake_helper", "wake_lead", "The scan is clean.")],
        )
        store = sqlite(":memory:")
        async with host(store=store, agents={"lead": lead}) as served:
            request = StartRunRequest.model_validate({"agent": "lead", "input": "Stand by."})
            run = await served.start_run(request, principal=ALICE, idempotency_key="k-1")
            assert isinstance(run, Ok), run
            branch = run.value.branch_id

            async def events() -> Sequence[Event]:
                return await events_of(store, ALICE.tenant, branch)

            await until(lambda: _turned(events, 1))
            team = await team_of(store, ALICE.tenant, branch)
            await operator_starts(store, team, ALICE, "wake_helper")
            await until(lambda: _turned(events, 2))

            all_events = await events()
            # The turn the mail opened is not a new run: the lead's log holds one user_input.
            assert sum(1 for e in all_events if isinstance(e, UserInputEvent)) == 1
            openers = [
                e for e in all_events if isinstance(e, MessageReceivedEvent | TurnCompletedEvent)
            ]
            assert isinstance(openers[1], MessageReceivedEvent)
            assert answers(all_events)[-1] == "The scan is clean."
        await assert_team_replays(await sq_of(scoped(store, ALICE.tenant)), team)

    asyncio.run(main())


async def _turned(events: Callable[[], Awaitable[Sequence[Event]]], count: int) -> bool:
    return turns(await events()) == count


def signed(body: JsonValue) -> RawRequest:
    now = int(time.time())
    raw = json.dumps(body).encode()
    mac = hmac.new(SECRET.encode(), f"v0:{now}:".encode() + raw, hashlib.sha256).hexdigest()
    headers = {"x-slack-request-timestamp": str(now), "x-slack-signature": f"v0={mac}"}
    return RawRequest(headers | {"content-type": "application/json"}, raw)


def a_message(event_id: str, text: str) -> JsonValue:
    message: JsonValue = {"type": "message", "user": "U1", "text": text, "channel": "C1"}
    return {"type": "event_callback", "team_id": "T1", "event_id": event_id, "event": message}


def chatting(store: Store, bot: Agent[None, str], sent: list[str]) -> Host:
    """A host serving `bot` on a fake Slack, recording what it posts."""

    def post(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            sent.append(str(json.loads(request.content).get("text")))
        return httpx.Response(200, json={"ok": True, "channel": "C1", "ts": f"171.{len(sent)}"})

    channel = slack(
        signing_secret=secret("SLACK_SIGNING_SECRET"),
        bot_token=secret("SLACK_BOT_TOKEN"),
        agent="lead",
        transport=httpx.MockTransport(post),
    )
    return host(store=store, agents={"lead": bot}, channels={"slack": channel})


async def conversation(store: Store) -> BranchId:
    """The root branch of the tenant's only conversation."""
    sq = await open_store(scoped(store, TENANT))
    rows = await sq.tables.inbox_rows()
    assert rows, "no inbox row"
    root = await sq.root(rows[0].thread_id)
    assert isinstance(root, Ok), root
    return root.value


# A channel lead's woken turn is proved in TypeScript only (host/test/team-wake.test.ts). Here
# `open_team` cannot bind a lead whose thread pinned the channel send tool: its config_hash
# includes channel_send, which only the host's own channel binding can reproduce, so a Python
# process has no operator handle on a channel lead's team to start a member with. The host's wake
# path is the same for both (it reads the lead's mail, never its channel), and the API lead above
# covers it. Reported as a parity issue for lane 29D's Host.team().


async def _posted(sent: list[str], count: int) -> bool:
    return len(sent) == count


SCAN: JsonValue = {
    "content": [
        {
            "type": "tool_use",
            "call_id": "c1",
            "name": "spawn_agent",
            "input": {"agent": "scanner", "prompt": "Scan.", "background": True},
        }
    ],
    "stop_reason": "tool_use",
    "usage": USAGE,
}


def test_the_next_host_wakes_an_idle_channel_thread_and_the_answer_is_delivered() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        sent: list[str] = []

        def support(lead: Model, child: Model) -> Agent[None, str]:
            scanner = agent(name="scanner", model=child)
            return agent(name="lead", model=lead, subagents=[scanner])

        # The crash: the child never reports, so the turn's reply is never issued either.
        dead = support(answering(SCAN, say("Started.")), stalled(say("late")))
        crashed = chatting(store, dead, sent)
        await crashed.ready()
        answered = await crashed.receive("slack", signed(a_message("Ev01", "Scan in background.")))
        assert isinstance(answered, Ok), answered
        branch = await _started(store)

        async def events() -> Sequence[Event]:
            return await events_of(store, TENANT, branch)

        await until(lambda: _turned(events, 1))
        # The turn answered the channel; the child it spawned never reported.
        await until(lambda: _posted(sent, 1))
        await crashed.stop()
        # The dead host's lease runs out (its TTL is 30 s): the drills do the same.
        await expire_leases(store)

        lead = answering(say("The scan is clean."))
        child = answering(say("No vulnerable deps."))
        async with chatting(store, support(lead, child), sent) as served:
            assert served is not None
            await until(lambda: _posted(sent, 2))
            all_events = await events()
            assert sum(1 for e in all_events if isinstance(e, WokenEvent)) == 1
            assert sum(1 for e in all_events if isinstance(e, UserInputEvent)) == 1
            assert sent == ["Started.", "The scan is clean."]

    asyncio.run(main())


async def _started(store: Store) -> BranchId:
    """The conversation's branch, once its thread has been opened."""

    async def ready() -> bool:
        sq = await open_store(scoped(store, TENANT))
        rows = await sq.tables.inbox_rows()
        return bool(rows) and isinstance(await sq.root(rows[0].thread_id), Ok)

    await until(ready)
    return await conversation(store)
