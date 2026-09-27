"""`google.rpc.ErrorInfo`: MUST for the HTTP+JSON binding, SHOULD for JSON-RPC. The asymmetry is the
point - an HTTP error body without one we recognise is a response we cannot read, while a JSON-RPC
error's ErrorInfo is checked only when it is there. spec/schema/a2a/README.md records it.

Mirrors typescript/packages/a2a/test/error-info.test.ts."""

import asyncio

from client_kit import (
    REST,
    RPC,
    Answering,
    Echoing,
    http_error,
    json_response,
    sending,
)

from threadsai.a2a.protocol import Faulted, call, error_info


class TestTheErrorInfoTheBindingsAskFor:
    """MUST for HTTP+JSON, SHOULD for JSON-RPC: spec/schema/a2a/README.md records the asymmetry."""

    def test_an_http_error_with_no_error_info_is_unreadable(self) -> None:
        asyncio.run(self._test_an_http_error_with_no_error_info_is_unreadable())

    async def _test_an_http_error_with_no_error_info_is_unreadable(self) -> None:
        transport = Answering(json_response({"code": -32001, "message": "gone"}, status=404))
        answer = await call(REST, "GetTask", {"id": "task-1"}, sending(transport))
        assert isinstance(answer, Faulted)
        assert answer.fault.name == "InvalidAgentResponseError"
        assert "ErrorInfo" in answer.fault.message

    def test_an_http_error_whose_reason_names_the_error_is_that_error(self) -> None:
        asyncio.run(self._test_an_http_error_whose_reason_names_the_error_is_that_error())

    async def _test_an_http_error_whose_reason_names_the_error_is_that_error(self) -> None:
        transport = Answering(http_error("TaskNotFoundError", 404))
        answer = await call(REST, "GetTask", {"id": "task-1"}, sending(transport))
        assert isinstance(answer, Faulted)
        assert answer.fault.name == "TaskNotFoundError"

    def test_a_reason_that_disagrees_with_the_status_is_refused(self) -> None:
        asyncio.run(self._test_a_reason_that_disagrees_with_the_status_is_refused())

    async def _test_a_reason_that_disagrees_with_the_status_is_refused(self) -> None:
        # TASK_NOT_FOUND is HTTP 404 in the pinned table, so a 400 carrying it is unreadable.
        transport = Answering(http_error("TaskNotFoundError", 400))
        answer = await call(REST, "GetTask", {"id": "task-1"}, sending(transport))
        assert isinstance(answer, Faulted)
        assert answer.fault.name == "InvalidAgentResponseError"

    def test_a_reason_outside_the_table_is_refused(self) -> None:
        asyncio.run(self._test_a_reason_outside_the_table_is_refused())

    async def _test_a_reason_outside_the_table_is_refused(self) -> None:
        transport = Answering(
            json_response(
                {
                    "code": -32001,
                    "message": "gone",
                    "details": [{"reason": "SOMETHING_ELSE", "domain": "partner.example"}],
                },
                status=404,
            )
        )
        answer = await call(REST, "GetTask", {"id": "task-1"}, sending(transport))
        assert isinstance(answer, Faulted)
        assert answer.fault.name == "InvalidAgentResponseError"
        assert "SOMETHING_ELSE" in answer.fault.message

    def test_a_json_rpc_error_needs_no_error_info(self) -> None:
        asyncio.run(self._test_a_json_rpc_error_needs_no_error_info())

    async def _test_a_json_rpc_error_needs_no_error_info(self) -> None:
        transport = Echoing({"error": {"code": -32002, "message": "already done"}})
        answer = await call(RPC, "CancelTask", {"id": "task-1"}, sending(transport))
        assert isinstance(answer, Faulted)
        assert answer.fault.name == "TaskNotCancelableError"

    def test_a_json_rpc_error_info_that_disagrees_with_the_code_is_refused(self) -> None:
        asyncio.run(self._test_a_json_rpc_error_info_that_disagrees_with_the_code_is_refused())

    async def _test_a_json_rpc_error_info_that_disagrees_with_the_code_is_refused(self) -> None:
        transport = Echoing(
            {
                "error": {
                    "code": -32002,
                    "message": "already done",
                    "data": error_info("TaskNotFoundError"),
                }
            }
        )
        answer = await call(RPC, "CancelTask", {"id": "task-1"}, sending(transport))
        assert isinstance(answer, Faulted)
        assert answer.fault.name == "InvalidAgentResponseError"

    def test_a_json_rpc_error_info_that_agrees_is_the_error_both_name(self) -> None:
        asyncio.run(self._test_a_json_rpc_error_info_that_agrees_is_the_error_both_name())

    async def _test_a_json_rpc_error_info_that_agrees_is_the_error_both_name(self) -> None:
        transport = Echoing(
            {
                "error": {
                    "code": -32002,
                    "message": "already done",
                    "data": error_info("TaskNotCancelableError"),
                }
            }
        )
        answer = await call(RPC, "CancelTask", {"id": "task-1"}, sending(transport))
        assert isinstance(answer, Faulted)
        assert answer.fault.name == "TaskNotCancelableError"
