"""The JSON-RPC 2.0 binding's envelope. PascalCase method names matching the gRPC service, an id
that is a string, a number or null, and `params` that is the operation's own request message.
Streaming operations answer SSE whose items are `{jsonrpc, id, result: <StreamResponse>}`."""

import json

from pydantic import JsonValue

from threads.a2a.protocol.errors import A2aFault, error_by_code, fault, json_rpc_code
from threads.result import Err, Ok

type RpcId = str | float | int | None


def rpc_result(request_id: RpcId, result: JsonValue) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})


def rpc_fault(request_id: RpcId, f: A2aFault) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": json_rpc_code(f.name), "message": f"{f.name}: {f.message}"},
        }
    )


def is_envelope(body: JsonValue) -> bool:
    """Both bindings can answer either way, so a JSON-RPC envelope is unwrapped wherever it
    appears."""
    return isinstance(body, dict) and body.get("jsonrpc") == "2.0"


def rpc_outcome(body: JsonValue) -> Ok[JsonValue] | Err[A2aFault]:
    """A peer's JSON-RPC answer: its result, or the A2A error it names."""
    if not isinstance(body, dict):
        return Err(fault("InvalidAgentResponseError", "not a JSON-RPC 2.0 response"))
    error = body.get("error")
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


def _fault_of(error: dict[str, JsonValue]) -> A2aFault:
    """A peer's error as one of ours. A code outside the table is an InternalError we record."""
    raw = error.get("code")
    code = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
    named = None if code is None else error_by_code(code)
    message = error.get("message")
    text = message if isinstance(message, str) else ""
    if named is None:
        return fault("InternalError", f"the peer answered JSON-RPC {raw}: {text}")
    return fault(named, text)
