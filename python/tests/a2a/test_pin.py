"""Pinning a partner's card: every way a card can be unusable is a value that names the fix, never a
raise, and a card's bytes verify with no network so replay stays hermetic.

The TypeScript side pins the same cases; the two must agree on which failure each card earns."""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.a2a_v1 import SendMessageResponse, StreamResponse, Task
from threads.a2a.protocol import (
    IDEMPOTENT_SEND,
    MAX_CARD_BYTES,
    MessagePayload,
    PinFailure,
    PinnedCard,
    Sending,
    StatusPayload,
    TaskPayload,
    fetch_card,
    is_file_part,
    is_interrupted,
    is_settled,
    is_terminal,
    payload_of,
    pin_card,
    stream_payload,
    text_of,
)
from threads.log.digest import sha256_hex
from threads.result import Err, Ok
from threads.web.guard import Resolve, Target
from threads.web.http import Fence, Request, Response, WebError

CARD_URL: Final = "https://partner.example/.well-known/agent-card.json"
DAY_MS: Final = 86_400_000
"""The window our own exposed agents declare, so the vector and this test read the same number."""

_RPC: Final = {
    "url": "https://partner.example/a2a/refunds",
    "protocolBinding": "JSONRPC",
    "protocolVersion": "1.0",
}
_REST: Final = {
    "url": "https://partner.example/a2a/json",
    "protocolBinding": "HTTP+JSON",
    "protocolVersion": "1.0",
}


def _card(**more: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "refunds",
        "description": "The partner's refunds desk.",
        "supportedInterfaces": [_REST, _RPC],
        "version": "1.0.0",
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "refunds",
                "name": "refunds",
                "description": "The partner's refunds desk.",
                "tags": ["refunds"],
            }
        ],
    }
    return {**base, **more}


def _bytes(card: dict[str, object]) -> bytes:
    return json.dumps(card).encode()


async def _public(_host: str, _port: int) -> Sequence[str]:
    return ("93.184.216.34",)


async def _private(_host: str, _port: int) -> Sequence[str]:
    return ("127.0.0.1",)


@dataclass(slots=True)
class _Serving:
    response: Response
    sent: list[Request] = field(default_factory=list[Request])

    async def send(
        self, target: Target, request: Request, fence: Fence, max_bytes: int
    ) -> Ok[Response] | Err[WebError]:
        assert await fence()
        assert target.scheme == "https"
        self.sent.append(request)
        if self.response.truncated or len(self.response.body) <= max_bytes:
            return Ok(self.response)
        return Ok(
            Response(
                self.response.status,
                self.response.headers,
                self.response.body[:max_bytes],
                truncated=True,
            )
        )


def _serving(body: bytes, *, status: int = 200) -> _Serving:
    return _Serving(Response(status, {"content-type": "application/json"}, body))


def _sending(transport: _Serving, *, resolve: Resolve = _public) -> Sending:
    return Sending(timeout_ms=5_000, transport=transport, resolve=resolve)


class TestAGoodCard:
    def test_the_bytes_are_kept_and_hashed_so_a_later_change_moves_nothing(self) -> None:
        raw = _bytes(_card())
        pinned = pin_card(raw, CARD_URL, has_bearer=False)
        assert isinstance(pinned, PinnedCard)
        assert pinned.bytes_ == raw
        assert pinned.sha256 == sha256_hex(raw)

    def test_json_rpc_wins_when_a_card_offers_both_bindings(self) -> None:
        # The card lists HTTP+JSON first; we prefer JSON-RPC regardless of card order.
        pinned = pin_card(_bytes(_card()), CARD_URL, has_bearer=False)
        assert isinstance(pinned, PinnedCard)
        assert pinned.wire.binding == "JSONRPC"
        assert pinned.wire.url == "https://partner.example/a2a/refunds"

    def test_only_a_1_0_interface_is_chosen(self) -> None:
        card = _card(supportedInterfaces=[{**_RPC, "protocolVersion": "1.0.4"}])
        pinned = pin_card(_bytes(card), CARD_URL, has_bearer=False)
        assert isinstance(pinned, PinnedCard)
        assert pinned.wire.binding == "JSONRPC"

    def test_our_extension_gives_the_window_and_nothing_else_does(self) -> None:
        with_ext = _card(
            capabilities={
                "streaming": False,
                "extensions": [{"uri": IDEMPOTENT_SEND, "params": {"window_ms": DAY_MS}}],
            }
        )
        pinned = pin_card(_bytes(with_ext), CARD_URL, has_bearer=False)
        assert isinstance(pinned, PinnedCard)
        assert pinned.dedup_window_ms == DAY_MS
        assert pinned.streaming is False

        other = _card(
            capabilities={
                "extensions": [
                    {"uri": "https://example.com/ext/other/v1", "params": {"window_ms": 1}}
                ]
            }
        )
        unpinned = pin_card(_bytes(other), CARD_URL, has_bearer=False)
        assert isinstance(unpinned, PinnedCard)
        # A peer is trusted to deduplicate only when its card declares OUR extension.
        assert unpinned.dedup_window_ms is None

    def test_a_window_that_is_not_a_positive_integer_is_no_window(self) -> None:
        for value in (0, -1, "86400000", 1.5, True):
            card = _card(
                capabilities={
                    "extensions": [{"uri": IDEMPOTENT_SEND, "params": {"window_ms": value}}]
                }
            )
            pinned = pin_card(_bytes(card), CARD_URL, has_bearer=False)
            assert isinstance(pinned, PinnedCard), value
            assert pinned.dedup_window_ms is None, value


class TestARefusedCard:
    def test_bytes_that_are_not_json_are_remote_unavailable(self) -> None:
        failed = pin_card(b"<html>nope</html>", CARD_URL, has_bearer=False)
        assert isinstance(failed, PinFailure)
        assert failed.code == "remote_unavailable"

    def test_a_card_with_no_skill_tags_is_refused_because_tags_are_required(self) -> None:
        card = _card(
            skills=[{"id": "refunds", "name": "refunds", "description": "d"}],
        )
        failed = pin_card(_bytes(card), CARD_URL, has_bearer=False)
        assert isinstance(failed, PinFailure)
        assert failed.code == "remote_unavailable"

    def test_an_extra_field_is_refused_rather_than_stored_as_ours(self) -> None:
        failed = pin_card(_bytes(_card(surprise="value")), CARD_URL, has_bearer=False)
        assert isinstance(failed, PinFailure)
        assert failed.code == "remote_unavailable"

    def test_a_0_3_only_peer_is_remote_unsupported(self) -> None:
        card = _card(supportedInterfaces=[{**_RPC, "protocolVersion": "0.3"}])
        failed = pin_card(_bytes(card), CARD_URL, has_bearer=False)
        assert isinstance(failed, PinFailure)
        assert failed.code == "remote_unsupported"
        assert "0.3" in failed.message

    def test_a_grpc_only_peer_is_remote_unsupported(self) -> None:
        card = _card(supportedInterfaces=[{**_RPC, "protocolBinding": "GRPC"}])
        failed = pin_card(_bytes(card), CARD_URL, has_bearer=False)
        assert isinstance(failed, PinFailure)
        assert failed.code == "remote_unsupported"
        assert "GRPC" in failed.message

    def test_a_card_declaring_only_oauth2_is_remote_auth_unsupported(self) -> None:
        card = _card(securitySchemes={"corp": {"oauth2SecurityScheme": {}}})
        failed = pin_card(_bytes(card), CARD_URL, has_bearer=True)
        assert isinstance(failed, PinFailure)
        assert failed.code == "remote_auth_unsupported"
        assert "bearer is the only scheme" in failed.message

    def test_a_card_asking_for_bearer_without_one_configured_names_the_fix(self) -> None:
        card = _card(securitySchemes={"tok": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}})
        failed = pin_card(_bytes(card), CARD_URL, has_bearer=False)
        assert isinstance(failed, PinFailure)
        assert failed.code == "remote_auth_unsupported"
        assert "bearer(secret" in failed.message

    def test_a_card_asking_for_bearer_with_one_configured_is_pinned(self) -> None:
        card = _card(securitySchemes={"tok": {"httpAuthSecurityScheme": {"scheme": "bearer"}}})
        pinned = pin_card(_bytes(card), CARD_URL, has_bearer=True)
        assert isinstance(pinned, PinnedCard)

    def test_a_card_declaring_no_scheme_asks_for_nothing(self) -> None:
        pinned = pin_card(_bytes(_card()), CARD_URL, has_bearer=False)
        assert isinstance(pinned, PinnedCard)


class TestFetchingACard:
    def test_an_http_card_url_is_never_fetched(self) -> None:
        transport = _serving(_bytes(_card()))
        failed = asyncio.run(
            fetch_card("http://partner.example/card.json", False, _sending(transport))
        )
        assert isinstance(failed, PinFailure)
        assert failed.code == "remote_unavailable"
        assert transport.sent == []

    def test_a_private_address_is_never_fetched(self) -> None:
        transport = _serving(_bytes(_card()))
        failed = asyncio.run(fetch_card(CARD_URL, False, _sending(transport, resolve=_private)))
        assert isinstance(failed, PinFailure)
        assert failed.code == "remote_unavailable"
        assert transport.sent == []

    def test_an_oversized_card_is_refused_before_it_is_parsed(self) -> None:
        card = _card(description="x" * (MAX_CARD_BYTES + 1))
        failed = asyncio.run(fetch_card(CARD_URL, False, _sending(_serving(_bytes(card)))))
        assert isinstance(failed, PinFailure)
        assert failed.code == "remote_unavailable"

    def test_a_card_served_over_https_is_pinned(self) -> None:
        pinned = asyncio.run(fetch_card(CARD_URL, False, _sending(_serving(_bytes(_card())))))
        assert isinstance(pinned, PinnedCard)


class TestReadingTheDataModel:
    def test_the_state_tables_come_from_the_pinned_spec(self) -> None:
        assert is_terminal("TASK_STATE_COMPLETED")
        assert is_terminal("TASK_STATE_REJECTED")
        assert not is_terminal("TASK_STATE_INPUT_REQUIRED")
        assert is_interrupted("TASK_STATE_INPUT_REQUIRED")
        assert is_interrupted("TASK_STATE_AUTH_REQUIRED")
        assert not is_interrupted("TASK_STATE_WORKING")
        assert is_settled("TASK_STATE_AUTH_REQUIRED")
        assert not is_settled("TASK_STATE_SUBMITTED")

    def test_a_oneof_with_none_or_several_set_is_not_read_as_either(self) -> None:
        task = Task.model_validate({"id": "t", "status": {"state": "TASK_STATE_WORKING"}})
        message: dict[str, object] = {"messageId": "m", "role": "ROLE_AGENT", "parts": []}
        assert isinstance(
            payload_of(SendMessageResponse.model_validate({"task": task.model_dump()})),
            TaskPayload,
        )
        assert isinstance(
            payload_of(SendMessageResponse.model_validate({"message": message})),
            MessagePayload,
        )
        assert payload_of(SendMessageResponse.model_validate({})) is None
        both = SendMessageResponse.model_validate({"task": task.model_dump(), "message": message})
        assert payload_of(both) is None

    def test_a_stream_item_reads_its_one_payload(self) -> None:
        status = {
            "taskId": "t",
            "contextId": "c",
            "status": {"state": "TASK_STATE_COMPLETED"},
        }
        item = StreamResponse.model_validate({"statusUpdate": status})
        assert isinstance(stream_payload(item), StatusPayload)
        assert stream_payload(StreamResponse.model_validate({})) is None

    def test_only_text_parts_are_shown_and_file_parts_are_named(self) -> None:
        parts = [
            {"text": "Refund "},
            {"data": {"id": 1}},
            {"text": "issued."},
        ]
        task = Task.model_validate(
            {
                "id": "t",
                "status": {
                    "state": "TASK_STATE_COMPLETED",
                    "message": {"messageId": "m", "role": "ROLE_AGENT", "parts": parts},
                },
            }
        )
        shown = task.status.message
        assert shown is not MISSING
        assert text_of(shown.parts) == "Refund issued."
        assert not any(is_file_part(p) for p in shown.parts)

        with_file = Task.model_validate(
            {
                "id": "t",
                "status": {
                    "state": "TASK_STATE_COMPLETED",
                    "message": {
                        "messageId": "m",
                        "role": "ROLE_AGENT",
                        "parts": [{"url": "https://partner.example/receipt.pdf"}],
                    },
                },
            }
        )
        file_message = with_file.status.message
        assert file_message is not MISSING
        assert all(is_file_part(p) for p in file_message.parts)
