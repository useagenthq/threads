"""Conformance runner for `intake` cases (spec/conformance/README.md): each webhook
goes through the host's intake pipeline with a fake verifying adapter, and the HTTP statuses
and the inbox rows, in insertion order, are compared."""

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import pytest
from corpus import CASES, cases, load
from pydantic import JsonValue, TypeAdapter

from threads import agent, scripted_model, sqlite
from threads.agents.store import open_store
from threads.host import (
    ChannelCapabilities,
    DeliveryOutcome,
    Inbound,
    Message,
    RawRequest,
    RawResponse,
    VerifiedDelivery,
    host,
)
from threads.host.channel import DeliveryError
from threads.log import Event, JsonObject, ParseError, Principal
from threads.loop.model import LookupResult, LookupUnknown
from threads.result import Err, Ok
from threads.secrets import Secret
from threads.store.sql import text_of

_WEBHOOK: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True, slots=True)
class FakeChannel:
    """Verifies a webhook by its JSON envelope unless it is `forged`; the tenant and each
    sender's issuer are `<channel>:<installation_id>`, and each item is keyed by its provider
    id, else `<delivery_id>#<index>` (spec/conformance/README.md, intake)."""

    name: str
    agent: str = "bot"
    capabilities: ChannelCapabilities = field(
        default_factory=lambda: ChannelCapabilities("none", False, False, False, False)
    )
    limits: Mapping[str, int] = field(default_factory=dict[str, int])
    secrets: Mapping[str, Secret] = field(default_factory=dict[str, Secret])

    def verify(self, raw: RawRequest) -> Ok[VerifiedDelivery] | Err[ParseError]:
        hook = _WEBHOOK.validate_json(raw.body)
        if hook.get("forged") is True:
            return Err(ParseError("unverified", "forged"))
        installation, delivery = hook["installation_id"], hook["delivery_id"]
        assert isinstance(installation, str)
        assert isinstance(delivery, str)
        return Ok(VerifiedDelivery(self.tenant(installation), installation, delivery))

    def tenant(self, installation: str) -> str:
        return f"{self.name}:{installation}"

    def parse(self, raw: RawRequest) -> Ok[Sequence[Inbound]] | Err[ParseError]:
        hook = _WEBHOOK.validate_json(raw.body)
        items = hook["items"]
        assert isinstance(items, list)
        parsed: list[Inbound] = []
        for index, item in enumerate(items):
            assert isinstance(item, dict)
            tenant = self.tenant(str(hook["installation_id"]))
            sender = Principal(issuer=tenant, tenant=tenant, subject=str(item["sender"]))
            key = item.get("item_id") or f"{hook['delivery_id']}#{index}"
            parsed.append(
                Message(
                    kind="message",
                    principal=sender,
                    address=str(item["conversation"]),
                    item_key=str(key),
                    content=str(item["text"]),
                )
            )
        return Ok(parsed)

    def ack(self, raw: RawRequest) -> RawResponse:
        return RawResponse(200, {}, b"")

    def render_text(self, text: str) -> Sequence[JsonObject]:
        return ({"text": text},)

    def render(self, event: Event) -> Sequence[JsonObject]:
        return ()

    async def perform(
        self, op: JsonObject, effect_key: str, credentials: Mapping[str, str]
    ) -> DeliveryOutcome:
        return DeliveryError("permanent", "definite_not_sent")

    async def lookup(self, effect_key: str, op: JsonObject) -> LookupResult[str]:
        return LookupUnknown("fake")


@pytest.mark.parametrize("name", cases("intake"))
def test_intake_case(name: str) -> None:
    case, expected = load(CASES / name, "case.json"), load(CASES / name, "expected.json")
    given = case["input"]
    assert isinstance(given, dict)
    webhooks = given["webhooks"]
    assert isinstance(webhooks, list)
    channels = {str(w["channel"]) for w in webhooks if isinstance(w, dict)}
    replies = [text_reply() for _ in range(16)]
    bot = agent(model=scripted_model({"responses": replies}))
    store = sqlite(":memory:")

    async def main() -> tuple[list[int], list[JsonValue], int]:
        served = host(
            store=store, agents={"bot": bot}, channels={c: FakeChannel(c) for c in channels}
        )
        statuses: list[int] = []
        async with served:
            for hook in webhooks:
                assert isinstance(hook, dict)
                raw = RawRequest({}, json.dumps(hook).encode())
                answered = await served.receive(str(hook["channel"]), raw)
                statuses.append(answered.value.status if isinstance(answered, Ok) else 401)
                assert isinstance(answered, Ok) or answered.error.code == "unverified"
        sq = await open_store(store)
        rows = await sq.run(
            lambda c: c.execute(
                "SELECT channel, item_key, thread_id FROM inbox ORDER BY inbox_id"
            ).fetchall()
        )
        inbox: list[JsonValue] = [
            {"channel": text_of(ch), "item_key": text_of(key)} for ch, key, _ in rows
        ]
        return statuses, inbox, len({text_of(thread) for _, _, thread in rows})

    statuses, inbox, threads = asyncio.run(main())
    assert statuses == expected["responses"]
    assert inbox == expected["inbox"]
    if "threads" in expected:
        assert threads == expected["threads"]


def text_reply() -> JsonValue:
    usage: JsonValue = {"input_tokens": 1, "output_tokens": 1}
    return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": usage}
