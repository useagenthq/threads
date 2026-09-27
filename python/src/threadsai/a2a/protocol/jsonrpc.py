"""The JSON-RPC 2.0 binding's envelope. PascalCase method names matching the gRPC service, an id
that is a string, a number or null, and `params` that is the operation's own request message.
Streaming operations answer SSE whose items are `{jsonrpc, id, result: <StreamResponse>}`."""

import json

from pydantic import JsonValue

from threadsai.a2a.protocol.errors import (
    A2aFault,
    error_by_code,
    error_info,
    error_info_in,
    fault,
    json_rpc_code,
)
from threadsai.result import Err, Ok

type RpcId = str | float | int | None


def rpc_result(request_id: RpcId, result: JsonValue) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})


def rpc_fault(request_id: RpcId, f: A2aFault) -> str:
    """A refusal in the envelope. `data` carries the ErrorInfo, which the binding says a JSON-RPC
    error SHOULD have: the code already names the error, so this is the same name said the way the
    HTTP+JSON binding says it, and a peer that reads only one of the two still agrees with us."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": json_rpc_code(f.name),
                "message": f"{f.name}: {f.message}",
                "data": error_info(f.name),
            },
        }
    )


def is_envelope(body: JsonValue) -> bool:
    """Both bindings can answer either way, so a JSON-RPC envelope is unwrapped wherever it
    appears."""
    return isinstance(body, dict) and body.get("jsonrpc") == "2.0"


def rpc_outcome(body: JsonValue, sent: str | None) -> Ok[JsonValue] | Err[A2aFault]:
    """A peer's JSON-RPC answer: its result, or the A2A error it names.

    `sent` is the id we put on the request, and an answer is only ours if it carries that id back: a
    response is a partner's bytes, so it proves it answers us before we read anything out of it.
    None means we sent no id (the HTTP+JSON binding), so there is nothing to correlate."""
    if not isinstance(body, dict):
        return Err(fault("InvalidAgentResponseError", "not a JSON-RPC 2.0 response"))
    error = body.get("error")
    is_error = isinstance(error, dict)
    stray = _uncorrelated(sent, body, is_error=is_error)
    if stray is not None:
        return Err(stray)
    if isinstance(error, dict):
        return Err(_fault_of(error))
    if "result" not in body:
        return Err(
            fault(
                "InvalidAgentResponseError",
                "a JSON-RPC response carries a result or an error",
            )
        )
    return Ok(body["result"])


def _uncorrelated(
    sent: str | None, body: dict[str, JsonValue], *, is_error: bool
) -> A2aFault | None:
    """Why an answer is not the answer to our request, or None when it is. JSON-RPC 2.0 requires a
    null id on an error whose request id could not be determined, so a null id on an *error* is
    correlated: it cannot forge a result, and refusing it would throw away the peer's real error."""
    if sent is None:
        return None
    got = body.get("id")
    if got == sent:
        return None
    if is_error and "id" in body and got is None:
        return None
    return fault(
        "InvalidAgentResponseError",
        f"the answer carries id {json.dumps(got)}, not the {json.dumps(sent)} we sent",
    )


def _fault_of(error: dict[str, JsonValue]) -> A2aFault:
    """A peer's error as one of ours. A code outside the table is an InternalError we record. An
    ErrorInfo in `data` is checked when there is one and left alone when there is not, because the
    binding only says a JSON-RPC error SHOULD carry it; one that disagrees with the code is a
    response we cannot read, not an error we can act on."""
    raw = error.get("code")
    code = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
    named = None if code is None else error_by_code(code)
    message = error.get("message")
    text = message if isinstance(message, str) else ""
    found = error_info_in(error.get("data"))
    if found is not None and named is not None and found.named != named:
        return fault(
            "InvalidAgentResponseError",
            f"the peer answered JSON-RPC {raw} with ErrorInfo reason {found.info.reason}",
        )
    if named is None:
        return fault("InternalError", f"the peer answered JSON-RPC {raw}: {text}")
    return fault(named, text)
