"""Approvals over a channel: only a button carrying the
challenge id answers it, only an approver's answer counts, a second press appends nothing, and
free text such as "yes" approves nothing. Answering resumes the run, whose reply goes out."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, JsonValue, TypeAdapter

from threads import RunContext, agent, scripted_model, sqlite, tool
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
from threads.log import (
    ApprovalGrantedEvent,
    Event,
    JsonObject,
    ParseError,
    Principal,
    UserInputEvent,
)
from threads.loop.model import LookupResult, LookupUnknown
from threads.result import Err, Ok
from threads.secrets import Secret
from threads.store.sql import text_of

TEAM = "T1"
REQUESTER = Principal(issuer="fake:T1", tenant=TEAM, subject="U1")
APPROVER = Principal(issuer="fake:T1", tenant=TEAM, subject="U9")
USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}
_ITEMS: TypeAdapter[list[Inbound]] = TypeAdapter(list[Inbound])


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


@dataclass
class ItemsChannel:
    """Delivers the Inbound items a test hands it, as one verified batch per webhook."""

    sent: list[JsonObject] = field(default_factory=list[JsonObject])
    agent: str = "bot"
    capabilities: ChannelCapabilities = field(
        default_factory=lambda: ChannelCapabilities("none", True, False, False, False)
    )
    limits: Mapping[str, int] = field(default_factory=dict[str, int])
    credentials: Mapping[str, Secret] = field(default_factory=dict[str, Secret])

    def verify(self, raw: RawRequest) -> Ok[VerifiedDelivery] | Err[ParseError]:
        return Ok(VerifiedDelivery(TEAM, TEAM, raw.headers["delivery"]))

    def parse(self, raw: RawRequest) -> Ok[Sequence[Inbound]] | Err[ParseError]:
        return Ok(_ITEMS.validate_json(raw.body))

    def ack(self, raw: RawRequest) -> RawResponse:
        return RawResponse(200, {}, b"")

    def render(self, event: Event) -> Sequence[JsonObject]:
        return ({"text": "reply"},) if event.type == "model_response" else ()

    async def perform(
        self, op: JsonObject, effect_key: str, credentials: Mapping[str, str]
    ) -> DeliveryOutcome:
        self.sent.append(op)
        return Sent(f"ts{len(self.sent)}")

    async def lookup(self, effect_key: str) -> LookupResult[str]:
        return LookupUnknown("fake")


def webhook(delivery: str, *items: JsonValue) -> RawRequest:
    return RawRequest({"delivery": delivery}, json.dumps(list(items)).encode())


def message(key: str, words: str) -> JsonValue:
    who: JsonValue = REQUESTER.model_dump()
    return {"kind": "message", "principal": who, "address": "C1", "item_key": key, "content": words}


def press(key: str, challenge: str, by: Principal) -> JsonValue:
    return {
        "kind": "decision",
        "principal": by.model_dump(),
        "address": "C1",
        "item_key": key,
        "challenge_id": challenge,
        "decision": "grant",
    }


class Note(BaseModel):
    text: str


async def until[T](probe: Callable[[], Awaitable[T | None]]) -> T:
    for _ in range(200):
        found = await probe()
        if found is not None:
            return found
        await asyncio.sleep(0.01)
    raise AssertionError("the host never got there")


def test_only_an_approvers_button_answers_and_a_second_press_appends_nothing() -> None:
    runs: list[str] = []

    async def send(args: Note, _ctx: RunContext[None]) -> str:
        runs.append(args.text)
        return "sent"

    use: JsonValue = {
        "content": [{"type": "tool_use", "call_id": "c1", "name": "send", "input": {"text": "x"}}],
        "stop_reason": "tool_use",
        "usage": USAGE,
    }
    send_tool = tool(name="send", description="Send.", input=Note, runs="host", execute=send)
    script: JsonValue = {"responses": [use, text("Sent."), text("You said yes.")]}
    bot = agent(model=scripted_model(script), tools=[send_tool], approvers=[APPROVER])
    channel = ItemsChannel()
    store = sqlite(":memory:")

    async def main() -> list[JsonValue]:
        async with host(store=store, agents={"bot": bot}, channels={"fake": channel}) as served:
            sq = await open_store(scoped(store, TEAM))

            async def challenge() -> str | None:
                rows = await sq.run(
                    lambda c: c.execute("SELECT challenge_id FROM approvals").fetchall()
                )
                return text_of(rows[0][0]) if rows else None

            async def settled(count: int) -> bool | None:
                rows = await sq.run(
                    lambda c: c.execute(
                        "SELECT count(*) FROM inbox WHERE consumed_seq IS NULL"
                    ).fetchone()
                )
                return True if rows is not None and rows[0] == count else None

            assert isinstance(
                await served.receive("fake", webhook("d1", message("m1", "send it"))), Ok
            )
            challenge_id = await until(challenge)
            await served.receive("fake", webhook("d2", message("m2", "yes")))
            await served.receive("fake", webhook("d3", press("b1", challenge_id, REQUESTER)))
            await until(lambda: settled(1))
            assert runs == []
            await served.receive("fake", webhook("d4", press("b2", challenge_id, APPROVER)))
            await served.receive("fake", webhook("d5", press("b3", challenge_id, APPROVER)))
            await until(lambda: settled(0))
            await until(lambda: _replies(channel, 2))
        return [dict(op) for op in channel.sent]

    sent = asyncio.run(main())
    assert runs == ["x"]
    assert sent == [{"text": "reply", "address": "C1"}] * 2
    asyncio.run(_one_grant_by_the_approver(store))


async def _replies(channel: ItemsChannel, count: int) -> bool | None:
    return True if len(channel.sent) >= count else None


async def _one_grant_by_the_approver(store: Store) -> None:
    sq = await open_store(scoped(store, TEAM))
    rows = await sq.tables.inbox_rows()
    root = await sq.root(rows[0].thread_id)
    assert isinstance(root, Ok)
    read = await sq.read(root.value, 0)
    assert isinstance(read, Ok)
    events = read.value.fold.events
    grants = [e for e in events if isinstance(e, ApprovalGrantedEvent)]
    assert [g.actor.principal for g in grants] == [APPROVER]
    inputs = [e.data.text for e in events if isinstance(e, UserInputEvent)]
    assert inputs == ["send it", "yes"]
