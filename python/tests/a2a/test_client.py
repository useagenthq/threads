"""The invariant-3 boundary: one request answers exactly one of five outcomes, and `NotSent` is the
only one that lets a caller send again on its own. Each test drives one decision, so widening
`NotSent` by a single case breaks a named test rather than passing quietly.

This mirrors typescript/packages/a2a/test/client.test.ts case for case: the two implementations must
agree on what "uncertain" means, or an effect repeats in one of them. What a peer's answer has to
prove before it is read is in test_answer.py."""

import asyncio
import json

from client_kit import (
    REST,
    RPC,
    TASK,
    Answering,
    Echoing,
    Failing,
    http_error,
    json_response,
    private_address,
    sending,
    stream_response,
)

from threads.a2a.protocol import (
    Answered,
    Faulted,
    NotSent,
    Streamed,
    Uncertain,
    Wire,
    call,
    fetch_bytes,
)
from threads.web.http import Response


class TestAnswered:
    def test_json_rpc_result_is_unwrapped(self) -> None:
        asyncio.run(self._test_json_rpc_result_is_unwrapped())

    async def _test_json_rpc_result_is_unwrapped(self) -> None:
        transport = Echoing({"result": {"task": TASK}})
        answer = await call(RPC, "SendMessage", {"message": {}}, sending(transport))
        assert isinstance(answer, Answered)
        assert answer.value == {"task": TASK}

    def test_http_json_body_comes_as_it_came(self) -> None:
        asyncio.run(self._test_http_json_body_comes_as_it_came())

    async def _test_http_json_body_comes_as_it_came(self) -> None:
        transport = Answering(json_response(TASK))
        answer = await call(REST, "GetTask", {"id": "task-1"}, sending(transport))
        assert isinstance(answer, Answered)
        # A GET carries its fields in the query string, and the id in the path.
        target, request = transport.sent[0]
        assert "/tasks/task-1" in target.path
        assert request.body is None


class TestFaulted:
    def test_json_rpc_error_is_named_by_its_code(self) -> None:
        asyncio.run(self._test_json_rpc_error_is_named_by_its_code())

    async def _test_json_rpc_error_is_named_by_its_code(self) -> None:
        transport = Echoing({"error": {"code": -32001, "message": "gone"}})
        answer = await call(RPC, "GetTask", {"id": "task-1"}, sending(transport))
        assert isinstance(answer, Faulted)
        assert answer.fault.name == "TaskNotFoundError"

    def test_http_error_status_carries_its_a2a_code(self) -> None:
        asyncio.run(self._test_http_error_status_carries_its_a2a_code())

    async def _test_http_error_status_carries_its_a2a_code(self) -> None:
        transport = Answering(http_error("UnsupportedOperationError", 400))
        answer = await call(REST, "SubscribeToTask", {"id": "task-1"}, sending(transport))
        assert isinstance(answer, Faulted)
        assert answer.fault.name == "UnsupportedOperationError"

    def test_a_code_outside_the_table_quotes_the_peer(self) -> None:
        asyncio.run(self._test_a_code_outside_the_table_quotes_the_peer())

    async def _test_a_code_outside_the_table_quotes_the_peer(self) -> None:
        transport = Echoing({"error": {"code": -31999, "message": "odd"}})
        answer = await call(RPC, "GetTask", {"id": "task-1"}, sending(transport))
        assert isinstance(answer, Faulted)
        assert answer.fault.name == "InternalError"
        assert "-31999" in answer.fault.message

    def test_an_unparsable_answer_is_a_fault_not_a_doubt(self) -> None:
        asyncio.run(self._test_an_unparsable_answer_is_a_fault_not_a_doubt())

    async def _test_an_unparsable_answer_is_a_fault_not_a_doubt(self) -> None:
        # The peer replied, so nothing is in doubt about whether it received us.
        transport = Answering(Response(200, {"content-type": "application/json"}, b"not json"))
        answer = await call(RPC, "SendMessage", {"message": {}}, sending(transport))
        assert isinstance(answer, Faulted)
        assert answer.fault.name == "JSONParseError"


class TestNotSent:
    def test_a_connect_failure_proves_nothing_left(self) -> None:
        asyncio.run(self._test_a_connect_failure_proves_nothing_left())

    async def _test_a_connect_failure_proves_nothing_left(self) -> None:
        transport = Failing("unavailable", was_sent=False)
        answer = await call(RPC, "SendMessage", {"message": {}}, sending(transport))
        assert isinstance(answer, NotSent)

    def test_a_lost_lease_never_wrote_a_byte(self) -> None:
        asyncio.run(self._test_a_lost_lease_never_wrote_a_byte())

    async def _test_a_lost_lease_never_wrote_a_byte(self) -> None:
        transport = Failing("stale_epoch", was_sent=False)
        answer = await call(RPC, "SendMessage", {"message": {}}, sending(transport))
        assert isinstance(answer, NotSent)

    def test_a_non_https_url_is_not_sent_and_nothing_is_dialled(self) -> None:
        asyncio.run(self._test_a_non_https_url_is_not_sent_and_nothing_is_dialled())

    async def _test_a_non_https_url_is_not_sent_and_nothing_is_dialled(self) -> None:
        transport = Answering(json_response(TASK))
        answer = await call(
            Wire("http://partner.example/a2a", "JSONRPC"),
            "SendMessage",
            {"message": {}},
            sending(transport),
        )
        assert isinstance(answer, NotSent)
        assert transport.sent == []

    def test_an_address_the_guard_refuses_is_not_sent_and_nothing_is_dialled(self) -> None:
        asyncio.run(self._test_an_address_the_guard_refuses_is_not_sent_and_nothing_is_dialled())

    async def _test_an_address_the_guard_refuses_is_not_sent_and_nothing_is_dialled(self) -> None:
        transport = Answering(json_response(TASK))
        answer = await call(
            RPC, "SendMessage", {"message": {}}, sending(transport, resolve=private_address)
        )
        assert isinstance(answer, NotSent)
        assert transport.sent == []


class TestUncertain:
    def test_a_failure_after_dispatch_is_uncertain_never_not_sent(self) -> None:
        asyncio.run(self._test_a_failure_after_dispatch_is_uncertain_never_not_sent())

    async def _test_a_failure_after_dispatch_is_uncertain_never_not_sent(self) -> None:
        transport = Failing("unavailable", was_sent=True)
        answer = await call(RPC, "SendMessage", {"message": {}}, sending(transport))
        assert isinstance(answer, Uncertain)
        assert answer.reason == "transport_error"

    def test_a_timeout_is_uncertain_with_its_own_reason(self) -> None:
        asyncio.run(self._test_a_timeout_is_uncertain_with_its_own_reason())

    async def _test_a_timeout_is_uncertain_with_its_own_reason(self) -> None:
        transport = Failing("timeout", was_sent=True)
        answer = await call(RPC, "SendMessage", {"message": {}}, sending(transport))
        assert isinstance(answer, Uncertain)
        assert answer.reason == "timeout"

    def test_a_truncated_body_is_refused_rather_than_read_as_an_answer(self) -> None:
        asyncio.run(self._test_a_truncated_body_is_refused_rather_than_read_as_an_answer())

    async def _test_a_truncated_body_is_refused_rather_than_read_as_an_answer(self) -> None:
        transport = Answering(
            Response(200, {"content-type": "application/json"}, b"{}", truncated=True)
        )
        answer = await call(RPC, "SendMessage", {"message": {}}, sending(transport))
        assert isinstance(answer, Faulted)
        assert answer.fault.name == "InvalidAgentResponseError"


class TestStreamed:
    def test_sse_items_parse_in_order(self) -> None:
        asyncio.run(self._test_sse_items_parse_in_order())

    async def _test_sse_items_parse_in_order(self) -> None:
        transport = Echoing(
            {},
            frames=(
                '{"jsonrpc":"2.0","id":{id},"result":{"task":' + json.dumps(TASK) + "}}",
                '{"jsonrpc":"2.0","id":{id},"result":{"statusUpdate":{"taskId":"task-1",'
                '"contextId":"ctx-1","status":{"state":"TASK_STATE_COMPLETED"}}}}',
            ),
        )
        answer = await call(RPC, "SendStreamingMessage", {"message": {}}, sending(transport))
        assert isinstance(answer, Streamed)
        first, second = answer.items
        assert first.task is not None
        assert second.statusUpdate is not None

    def test_an_item_that_is_not_a_stream_response_is_a_fault(self) -> None:
        asyncio.run(self._test_an_item_that_is_not_a_stream_response_is_a_fault())

    async def _test_an_item_that_is_not_a_stream_response_is_a_fault(self) -> None:
        # Never guessed at, and never a short tuple either: a caller that saw two items where the
        # peer sent three could not tell a corrupt stream from one that simply ended.
        body = (
            'data: {"task":' + json.dumps(TASK) + "}\n\n"
            'data: {"unknownField":1}\n\n'
            'data: {"message":{"messageId":"m","role":"ROLE_AGENT","parts":[{"text":"hi"}]}}\n\n'
        )
        transport = Answering(stream_response(body))
        answer = await call(REST, "SendStreamingMessage", {"message": {}}, sending(transport))
        assert isinstance(answer, Faulted)
        assert answer.fault.name == "InvalidAgentResponseError"

    def test_a_frame_that_is_not_json_is_a_fault(self) -> None:
        asyncio.run(self._test_a_frame_that_is_not_json_is_a_fault())

    async def _test_a_frame_that_is_not_json_is_a_fault(self) -> None:
        transport = Answering(stream_response("data: not json at all\n\n"))
        answer = await call(REST, "SendStreamingMessage", {"message": {}}, sending(transport))
        assert isinstance(answer, Faulted)


class TestWhatGoesOnTheWire:
    def test_every_request_declares_the_version_and_asks_for_a2ajson_response(self) -> None:
        asyncio.run(self._test_every_request_declares_the_version_and_asks_for_a2ajson_response())

    async def _test_every_request_declares_the_version_and_asks_for_a2ajson_response(self) -> None:
        transport = Echoing({"result": {"task": TASK}})
        await call(RPC, "SendMessage", {"message": {}}, sending(transport))
        _, request = transport.sent[0]
        assert request.headers["A2A-Version"] == "1.0"
        assert request.headers["Content-Type"] == "application/a2a+json"

    def test_the_credential_rides_in_the_header_only(self) -> None:
        asyncio.run(self._test_the_credential_rides_in_the_header_only())

    async def _test_the_credential_rides_in_the_header_only(self) -> None:
        extension = "https://threadsai.dev/a2a/ext/idempotent-send/v1"
        transport = Answering(json_response({"jsonrpc": "2.0", "id": 1, "result": {"task": TASK}}))
        await call(
            RPC,
            "SendMessage",
            {"message": {}},
            sending(transport, authorization="Bearer shhh", extensions=(extension,)),
        )
        _, request = transport.sent[0]
        assert request.headers["Authorization"] == "Bearer shhh"
        assert request.headers["A2A-Extensions"] == extension
        assert b"shhh" not in (request.body or b"")

    def test_a_subscribe_is_a_get_as_the_normative_proto_annotates_it(self) -> None:
        asyncio.run(self._test_a_subscribe_is_a_get_as_the_normative_proto_annotates_it())

    async def _test_a_subscribe_is_a_get_as_the_normative_proto_annotates_it(self) -> None:
        transport = Answering(stream_response(""))
        await call(REST, "SubscribeToTask", {"id": "task-1"}, sending(transport))
        target, request = transport.sent[0]
        assert request.method == "GET"
        assert "/tasks/task-1:subscribe" in target.path


class TestFetchingACard:
    def test_no_credential_is_sent_because_discovery_is_unauthenticated(self) -> None:
        asyncio.run(self._test_no_credential_is_sent_because_discovery_is_unauthenticated())

    async def _test_no_credential_is_sent_because_discovery_is_unauthenticated(self) -> None:
        transport = Answering(json_response({"name": "refunds"}))
        await fetch_bytes(
            "https://partner.example/.well-known/agent-card.json",
            4096,
            sending(transport, authorization="Bearer shhh"),
        )
        _, request = transport.sent[0]
        assert "Authorization" not in request.headers

    def test_a_body_over_the_cap_fails_rather_than_being_truncated_into_a_card(self) -> None:
        asyncio.run(self._test_a_body_over_the_cap_fails_rather_than_being_truncated_into_a_card())

    async def _test_a_body_over_the_cap_fails_rather_than_being_truncated_into_a_card(self) -> None:
        transport = Answering(
            Response(200, {"content-type": "application/json"}, b"{}", truncated=True)
        )
        got = await fetch_bytes("https://partner.example/card.json", 32, sending(transport))
        assert got.bytes_ is None
        assert got.why is not None
        assert "32 bytes" in got.why

    def test_a_non_200_is_a_failure_that_names_the_status(self) -> None:
        asyncio.run(self._test_a_non_200_is_a_failure_that_names_the_status())

    async def _test_a_non_200_is_a_failure_that_names_the_status(self) -> None:
        transport = Answering(json_response({}, status=503))
        got = await fetch_bytes("https://partner.example/card.json", 4096, sending(transport))
        assert got.bytes_ is None
        assert got.why == "HTTP 503"
