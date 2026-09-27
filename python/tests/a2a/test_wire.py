"""The bytes one call becomes.

These are byte-exact on purpose. The same literals are asserted in
typescript/packages/a2a/test/wire.test.ts, which is what makes cross-language byte identity a
contract rather than a coincidence: Python's default `json.dumps` separators put a space after every
comma and colon, so the two languages used to write different bytes for the same call."""

from pydantic import JsonValue

from threadsai.a2a.protocol import Wire, outbound
from threadsai.a2a.protocol.wire import sent_rpc_id

RPC = Wire("https://partner.example/a2a/refunds", "JSONRPC")
REST = Wire("https://partner.example/a2a/refunds", "HTTP+JSON")

MESSAGE: JsonValue = {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "hi"}]}


class TestTheEnvelopeBytes:
    def test_a_json_rpc_body_is_written_the_way_json_stringify_writes_it(self) -> None:
        built = outbound(RPC, "SendMessage", {"message": MESSAGE}, "req-1")
        assert built.body == (
            '{"jsonrpc":"2.0","id":"req-1","method":"SendMessage","params":'
            '{"message":{"messageId":"m1","role":"ROLE_USER","parts":[{"text":"hi"}]}}}'
        )

    def test_the_id_we_sent_comes_back_out_for_the_caller_to_correlate(self) -> None:
        assert outbound(RPC, "SendMessage", {"message": MESSAGE}, "req-1").rpc_id == "req-1"

    def test_an_http_json_body_is_the_request_message_alone(self) -> None:
        built = outbound(REST, "SendMessage", {"message": MESSAGE}, "req-1")
        assert built.body == (
            '{"message":{"messageId":"m1","role":"ROLE_USER","parts":[{"text":"hi"}]}}'
        )

    def test_an_http_json_request_carries_no_envelope_so_there_is_no_id(self) -> None:
        # Nothing to correlate: the binding has no envelope to put an id in.
        assert outbound(REST, "SendMessage", {"message": MESSAGE}, "req-1").rpc_id is None

    def test_a_get_puts_its_non_string_fields_in_the_query_as_compact_json(self) -> None:
        built = outbound(REST, "ListTasks", {"pageSize": 10}, "req-1")
        assert built.body is None
        assert "pageSize=10" in built.url


class TestTheIdAnAnswerHasToCarryBack:
    def test_it_is_this_requests_own_id_when_nothing_replaces_the_body(self) -> None:
        built = outbound(RPC, "SendMessage", {"message": MESSAGE}, "req-1")
        assert sent_rpc_id(built, None) == "req-1"

    def test_it_is_the_stored_bodys_id_when_a_re_dispatch_replays_it(self) -> None:
        # A re-dispatch sends the bytes its first attempt stored, so correlating against an id
        # generated for this attempt would refuse the peer's answer to the request we actually sent.
        stored = outbound(RPC, "SendMessage", {"message": MESSAGE}, "stored-1").body
        assert stored is not None
        fresh = outbound(RPC, "SendMessage", {"message": MESSAGE}, "fresh-2")
        assert sent_rpc_id(fresh, stored) == "stored-1"

    def test_it_is_nothing_on_http_json_which_has_no_envelope(self) -> None:
        rest = outbound(REST, "SendMessage", {"message": MESSAGE}, "req-1")
        assert sent_rpc_id(rest, None) is None
        assert sent_rpc_id(rest, '{"id":"not-an-envelope-id"}') is None

    def test_it_is_nothing_when_a_replayed_body_carries_no_string_id(self) -> None:
        built = outbound(RPC, "SendMessage", {"message": MESSAGE}, "req-1")
        assert sent_rpc_id(built, "not json") is None
        assert sent_rpc_id(built, '{"id":7}') is None
