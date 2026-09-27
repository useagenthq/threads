"""Reading a peer's answer. A response is a partner's bytes, so every case here asks the same
question: what does this answer have to prove before we read anything out of it? It has to be ours,
and it has to be decodable.

Mirrors typescript/packages/a2a/test/answer.test.ts."""

import asyncio
import json

from client_kit import (
    REST,
    RPC,
    TASK,
    Answering,
    Echoing,
    json_response,
    sending,
    sent_id,
)

from threads.a2a.protocol import Answered, Faulted, call
from threads.web.http import Response


class TestAnAnswerHasToProveItAnswersUs:
    """A response is a partner's bytes, so it proves it answers the request we sent before we read
    anything out of it. Mirrors typescript/packages/a2a/test/client.test.ts."""

    def test_a_result_carrying_someone_elses_id_is_refused(self) -> None:
        asyncio.run(self._test_a_result_carrying_someone_elses_id_is_refused())

    async def _test_a_result_carrying_someone_elses_id_is_refused(self) -> None:
        transport = Answering(
            json_response({"jsonrpc": "2.0", "id": "not-our-id", "result": {"task": TASK}})
        )
        answer = await call(RPC, "SendMessage", {"message": {}}, sending(transport))
        assert isinstance(answer, Faulted)
        assert answer.fault.name == "InvalidAgentResponseError"
        assert "not-our-id" in answer.fault.message

    def test_the_id_is_fresh_per_request(self) -> None:
        asyncio.run(self._test_the_id_is_fresh_per_request())

    async def _test_the_id_is_fresh_per_request(self) -> None:
        transport = Echoing({"result": {"task": TASK}})
        await call(RPC, "SendMessage", {"message": {}}, sending(transport))
        await call(RPC, "SendMessage", {"message": {}}, sending(transport))
        first, second = (sent_id(request) for _target, request in transport.sent)
        assert isinstance(first, str)
        assert first != second

    def test_an_sse_frame_carrying_someone_elses_id_is_refused(self) -> None:
        asyncio.run(self._test_an_sse_frame_carrying_someone_elses_id_is_refused())

    async def _test_an_sse_frame_carrying_someone_elses_id_is_refused(self) -> None:
        transport = Echoing(
            {},
            frames=(
                '{"jsonrpc":"2.0","id":{id},"result":{"task":' + json.dumps(TASK) + "}}",
                '{"jsonrpc":"2.0","id":"not-our-id","result":{"statusUpdate":{"taskId":"t",'
                '"contextId":"c","status":{"state":"TASK_STATE_COMPLETED"}}}}',
            ),
        )
        answer = await call(RPC, "SendStreamingMessage", {"message": {}}, sending(transport))
        assert isinstance(answer, Faulted)
        assert "not-our-id" in answer.fault.message

    def test_a_null_id_on_an_error_is_ours(self) -> None:
        asyncio.run(self._test_a_null_id_on_an_error_is_ours())

    async def _test_a_null_id_on_an_error_is_ours(self) -> None:
        # JSON-RPC 2.0 requires a null id when the request's id could not be determined, so refusing
        # it would throw away the peer's real error. An error cannot forge a result.
        transport = Answering(
            json_response(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "could not read your id"},
                }
            )
        )
        answer = await call(RPC, "SendMessage", {"message": {}}, sending(transport))
        assert isinstance(answer, Faulted)
        assert answer.fault.name == "JSONParseError"

    def test_a_null_id_on_a_result_is_not_ours(self) -> None:
        asyncio.run(self._test_a_null_id_on_a_result_is_not_ours())

    async def _test_a_null_id_on_a_result_is_not_ours(self) -> None:
        transport = Answering(
            json_response({"jsonrpc": "2.0", "id": None, "result": {"task": TASK}})
        )
        answer = await call(RPC, "SendMessage", {"message": {}}, sending(transport))
        assert isinstance(answer, Faulted)
        assert answer.fault.name == "InvalidAgentResponseError"

    def test_an_http_json_answer_has_no_envelope_to_correlate(self) -> None:
        asyncio.run(self._test_an_http_json_answer_has_no_envelope_to_correlate())

    async def _test_an_http_json_answer_has_no_envelope_to_correlate(self) -> None:
        transport = Answering(json_response(TASK))
        answer = await call(REST, "GetTask", {"id": "task-1"}, sending(transport))
        assert isinstance(answer, Answered)


class TestABodyThatIsNotUtf8:
    def test_is_a_fault_not_replacement_characters(self) -> None:
        asyncio.run(self._test_is_a_fault_not_replacement_characters())

    async def _test_is_a_fault_not_replacement_characters(self) -> None:
        # Read on as U+FFFD it would parse, because U+FFFD is legal inside a JSON string, and the
        # corrupted text would be handed on as if the peer had sent it.
        transport = Answering(Response(200, {"content-type": "application/json"}, b'{"id":"\xff"}'))
        answer = await call(REST, "GetTask", {"id": "task-1"}, sending(transport))
        assert isinstance(answer, Faulted)
        assert "UTF-8" in answer.fault.message
