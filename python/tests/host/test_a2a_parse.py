"""What operation an inbound request names, in either binding, and how a refusal looks.

Every request here is built by hand rather than served, so the table itself is what is asserted: the
paths and verbs we answer, the ones we refuse by name because the card says we do not serve them,
and the ones that must look exactly like a path that does not exist. Mirrors the protocol half of
typescript/packages/host/test/a2a/protocol.test.ts and cuts.test.ts."""

import asyncio
import json
from typing import Final

import pytest
from pydantic import BaseModel, JsonValue
from starlette.requests import Request

from threads.a2a.protocol import Binding, Method
from threads.host.a2a.parse import Inbound, Named, Unnamed, operation, request_of, tenant_mismatch
from threads.host.a2a.wire import Envelope, refuse, unauthenticated
from threads.log import Principal
from threads.result import Err

ALICE: Final = Principal(issuer="partner", tenant="acme", subject="refunds.partner.example")
OK: Final = 200
BAD_REQUEST: Final = 400
NOT_FOUND: Final = 404
UNAUTHORIZED: Final = 401
RPC_ID: Final = 7
METHOD_NOT_FOUND: Final = -32601
PAGE_SIZE: Final = 25


def _request(verb: str, path: str, *, body: JsonValue | None = None, query: str = "") -> Request:
    raw = b"" if body is None else json.dumps(body).encode()
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": verb,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": [(b"content-type", b"application/json")],
        "scheme": "https",
        "server": ("host.test", 443),
        "client": ("198.51.100.4", 1234),
        "root_path": "",
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": raw, "more_body": False}

    # A hand-built ASGI scope: the table is what is asserted, so nothing is served.
    return Request(scope, receive)


def _parsed(
    verb: str,
    path: str,
    binding: Binding,
    *,
    body: JsonValue | None = None,
    query: str = "",
) -> Named | Unnamed:
    inbound = Inbound(_request(verb, path, body=body, query=query), "support", path, binding)
    return asyncio.run(operation(inbound))


def _message(message_id: str) -> JsonValue:
    return {
        "message": {
            "messageId": message_id,
            "role": "ROLE_USER",
            "parts": [{"text": "Where is order 1042?"}],
        }
    }


class TestTheJsonRpcBinding:
    def test_a_method_we_serve_is_named_with_its_params(self) -> None:
        got = _parsed(
            "POST",
            "",
            "JSONRPC",
            body={
                "jsonrpc": "2.0",
                "id": RPC_ID,
                "method": "SendMessage",
                "params": _message("m-1"),
            },
        )
        assert isinstance(got, Named)
        assert got.method == "SendMessage"
        assert got.envelope.id == RPC_ID

    def test_the_request_id_is_echoed_even_when_the_rest_is_unusable(self) -> None:
        # A client has to be able to match a refusal to the call it made.
        got = _parsed("POST", "", "JSONRPC", body={"jsonrpc": "2.0", "id": "abc", "method": "Nope"})
        assert isinstance(got, Unnamed)
        assert got.envelope.id == "abc"
        assert got.fault.name == "MethodNotFoundError"

    def test_a_body_that_is_not_json_rpc_is_an_invalid_request(self) -> None:
        got = _parsed("POST", "", "JSONRPC", body={"hello": "there"})
        assert isinstance(got, Unnamed)
        assert got.fault.name == "InvalidRequestError"

    def test_a_verb_other_than_post_is_not_an_operation(self) -> None:
        got = _parsed("GET", "", "JSONRPC")
        assert isinstance(got, Unnamed)
        assert got.fault.name == "MethodNotFoundError"


class TestTheHttpJsonBinding:
    @pytest.mark.parametrize(
        ("verb", "path", "method"),
        [
            ("POST", "/message:send", "SendMessage"),
            ("POST", "/message:stream", "SendStreamingMessage"),
            ("GET", "/tasks", "ListTasks"),
            ("GET", "/tasks/task-1", "GetTask"),
            ("POST", "/tasks/task-1:cancel", "CancelTask"),
            ("GET", "/tasks/task-1:subscribe", "SubscribeToTask"),
            # The proto annotates subscribe as GET and the prose table says POST; we accept both.
            ("POST", "/tasks/task-1:subscribe", "SubscribeToTask"),
        ],
    )
    def test_the_paths_we_serve(self, verb: str, path: str, method: Method) -> None:
        body = _message("m-1") if verb == "POST" and "message" in path else {}
        got = _parsed(verb, path, "HTTP+JSON", body=body)
        assert isinstance(got, Named), got
        assert got.method == method

    def test_a_task_id_reaches_the_params_from_the_path(self) -> None:
        got = _parsed("GET", "/tasks/task%2F1", "HTTP+JSON")
        assert isinstance(got, Named)
        assert got.params == {"id": "task/1"}

    def test_the_version_query_parameter_is_not_an_operation_field(self) -> None:
        # It is the version check's, and a request schema forbids what it does not declare.
        got = _parsed("GET", "/tasks", "HTTP+JSON", query="A2A-Version=1.0&contextId=c-1")
        assert isinstance(got, Named)
        assert got.params == {"contextId": "c-1"}

    def test_a_numeric_query_field_arrives_as_a_number(self) -> None:
        got = _parsed("GET", "/tasks", "HTTP+JSON", query=f"pageSize={PAGE_SIZE}")
        assert isinstance(got, Named)
        assert got.params == {"pageSize": PAGE_SIZE}

    @pytest.mark.parametrize(
        ("verb", "path"),
        [
            ("GET", "/message:send"),
            ("POST", "/tasks"),
            ("GET", "/nope"),
            ("GET", "/tasks/a/b"),
            # The {tenant}/... variants of every operation: not served, because our card declares
            # no AgentInterface.tenant.
            ("POST", "/acme/message:send"),
            ("GET", "/acme/tasks/task-1"),
        ],
    )
    def test_a_path_or_verb_that_is_none_of_ours_looks_like_a_missing_path(
        self, verb: str, path: str
    ) -> None:
        got = _parsed(verb, path, "HTTP+JSON", body={})
        assert isinstance(got, Unnamed)
        assert got.fault.name == "MethodNotFoundError"

    def test_a_body_that_is_not_an_object_is_refused(self) -> None:
        got = _parsed("POST", "/message:send", "HTTP+JSON", body=[1, 2])
        assert isinstance(got, Unnamed)
        assert got.fault.name == "InvalidRequestError"


class TestTheOperationsWeDoNotServe:
    @pytest.mark.parametrize(
        "path",
        ["/tasks/task-1/pushNotificationConfigs", "/tasks/task-1/pushNotificationConfigs/c-1"],
    )
    def test_a_push_notification_config_says_so_by_name(self, path: str) -> None:
        # The card says pushNotifications is false, so a client that calls anyway deserves the
        # specific refusal rather than a 404 that looks like a wrong path.
        got = _parsed("GET", path, "HTTP+JSON")
        assert isinstance(got, Unnamed)
        assert got.fault.name == "PushNotificationNotSupportedError"

    def test_the_extended_card_says_so_by_name(self) -> None:
        got = _parsed("GET", "/extendedAgentCard", "HTTP+JSON")
        assert isinstance(got, Unnamed)
        assert got.fault.name == "UnsupportedOperationError"

    def test_a_push_config_method_on_the_rpc_binding_says_the_same(self) -> None:
        got = _parsed(
            "POST",
            "",
            "JSONRPC",
            body={"jsonrpc": "2.0", "id": 1, "method": "CreateTaskPushNotificationConfig"},
        )
        assert isinstance(got, Unnamed)
        assert got.fault.name == "PushNotificationNotSupportedError"


def _parsed_request(method: Method, params: JsonValue) -> BaseModel:
    got = request_of(method, params)
    assert not isinstance(got, Err), got
    return got


class TestTheRequestMessage:
    def test_a_message_that_is_not_one_is_invalid_params(self) -> None:
        refused = request_of("SendMessage", {"message": {"role": "ROLE_USER"}})
        assert isinstance(refused, Err)
        assert refused.error.name == "InvalidParamsError"

    def test_a_field_the_proto_does_not_declare_is_refused(self) -> None:
        # Unknown fields are kept as provider data only where the proto allows them.
        assert isinstance(request_of("GetTask", {"id": "task-1", "surprise": 1}), Err)

    def test_a_tenant_is_accepted_only_as_the_callers_own(self) -> None:
        mine = _parsed_request("GetTask", {"id": "task-1", "tenant": "acme"})
        theirs = _parsed_request("GetTask", {"id": "task-1", "tenant": "other"})
        assert tenant_mismatch(mine, ALICE) is None
        wrong = tenant_mismatch(theirs, ALICE)
        assert wrong is not None
        assert wrong.name == "InvalidParamsError"

    def test_a_request_with_no_tenant_is_the_callers_own(self) -> None:
        assert tenant_mismatch(_parsed_request("GetTask", {"id": "task-1"}), ALICE) is None


class TestHowARefusalLooks:
    def test_json_rpc_answers_200_with_the_error_in_its_envelope(self) -> None:
        got = _parsed("POST", "", "JSONRPC", body={"jsonrpc": "2.0", "id": 1, "method": "Nope"})
        assert isinstance(got, Unnamed)
        response = refuse(got.envelope, got.fault)
        assert response.status_code == OK
        body = json.loads(bytes(response.body))
        assert isinstance(body, dict)
        assert body["error"]["code"] == METHOD_NOT_FOUND
        assert body["id"] == 1

    def test_http_json_answers_the_status_from_the_pinned_table(self) -> None:
        missing = _parsed("GET", "/nope", "HTTP+JSON")
        assert isinstance(missing, Unnamed)
        response = refuse(Envelope("HTTP+JSON"), missing.fault)
        assert response.status_code == NOT_FOUND
        body = json.loads(bytes(response.body))
        assert isinstance(body, dict)
        assert body["code"] == METHOD_NOT_FOUND

    def test_a_401_carries_a_bearer_challenge_in_both_bindings(self) -> None:
        for binding in ("JSONRPC", "HTTP+JSON"):
            response = unauthenticated(Envelope(binding))
            assert response.status_code == UNAUTHORIZED
            assert response.headers["www-authenticate"] == "Bearer"
