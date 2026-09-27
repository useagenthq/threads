"""What operation a request names, in either binding, and its request message. The envelope an
answer will use is settled before anything here can fail, so even a malformed body is refused in
the caller's own binding rather than in whichever one we guessed."""

from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, JsonValue, ValidationError
from starlette.requests import Request

from threads._generated.a2a_v1 import (
    CancelTaskRequest,
    GetTaskRequest,
    ListTasksRequest,
    SendMessageRequest,
    SubscribeToTaskRequest,
)
from threads.a2a.protocol import (
    HTTP,
    VERSION_HEADER,
    A2aFault,
    Binding,
    Method,
    fault,
    inbound_path,
    method_of,
    parse_json,
)
from threads.host.a2a.cuts import cut_operation, cut_path
from threads.host.a2a.wire import Envelope
from threads.log import Principal
from threads.result import Err


@dataclass(frozen=True, slots=True)
class Inbound:
    request: Request
    name: str
    path: str
    """The path under `/a2a/{agent}`; empty for the JSON-RPC endpoint."""
    binding: Binding


@dataclass(frozen=True, slots=True)
class Named:
    envelope: Envelope
    method: Method
    params: JsonValue


@dataclass(frozen=True, slots=True)
class Unnamed:
    envelope: Envelope
    fault: A2aFault


type Parsed = Named | Unnamed

_SCHEMAS: Final[dict[Method, type[BaseModel]]] = {
    "SendMessage": SendMessageRequest,
    "SendStreamingMessage": SendMessageRequest,
    "GetTask": GetTaskRequest,
    "ListTasks": ListTasksRequest,
    "CancelTask": CancelTaskRequest,
    "SubscribeToTask": SubscribeToTaskRequest,
}

_NOT_AN_OPERATION: Final = fault("MethodNotFoundError", "no such A2A operation on this host")

_NUMBERS: Final[frozenset[str]] = frozenset({"pageSize", "historyLength"})
"""The GET operations' non-string fields, as the request schemas declare them."""
_BOOLEANS: Final[frozenset[str]] = frozenset({"includeArtifacts"})


def request_of(method: Method, params: JsonValue) -> Err[A2aFault] | BaseModel:
    """The request message, parsed at the boundary, or why it is not one."""
    try:
        return _SCHEMAS[method].model_validate(params)
    except ValidationError as failed:
        return Err(fault("InvalidParamsError", f"{failed.error_count()} invalid field(s)"))


def tenant_mismatch(params: BaseModel, principal: Principal) -> A2aFault | None:
    """A request may carry the A2A tenant field only as the caller's own tenant."""
    tenant = getattr(params, "tenant", None)
    if not isinstance(tenant, str) or tenant == principal.tenant:
        return None
    return fault("InvalidParamsError", f"tenant {tenant} is not this caller's tenant")


async def operation(inbound: Inbound) -> Parsed:
    if inbound.binding == "JSONRPC":
        return await _rpc_operation(inbound)
    return await _http_operation(inbound)


async def _rpc_operation(inbound: Inbound) -> Parsed:
    bare = Envelope("JSONRPC")
    if inbound.request.method != "POST":
        return Unnamed(bare, _NOT_AN_OPERATION)
    body = parse_json((await inbound.request.body()).decode(errors="replace"))
    if isinstance(body, Err):
        return Unnamed(bare, body.error)
    envelope, method_name, params = _envelope_of(body.value)
    if method_name is None:
        return Unnamed(envelope, fault("InvalidRequestError", "not a JSON-RPC 2.0 request"))
    method = method_of(method_name)
    if method is not None:
        return Named(envelope, method, params)
    cut = cut_operation(method_name)
    return Unnamed(envelope, cut or fault("MethodNotFoundError", f"no method {method_name}"))


def _envelope_of(body: JsonValue) -> tuple[Envelope, str | None, JsonValue]:
    """The envelope a JSON-RPC answer will use, and what the request named. The id is echoed even
    when the rest of the request is unusable, so a client can match the refusal to its call."""
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return (Envelope("JSONRPC"), None, {})
    raw_id = body.get("id")
    request_id = raw_id if isinstance(raw_id, str | int | float) else None
    envelope = Envelope("JSONRPC", request_id)
    name = body.get("method")
    return (envelope, name if isinstance(name, str) else None, body.get("params") or {})


async def _http_operation(inbound: Inbound) -> Parsed:
    bare = Envelope("HTTP+JSON")
    cut = cut_path(inbound.path, inbound.request.method)
    if cut is not None:
        return Unnamed(bare, cut)
    found = inbound_path(inbound.path)
    if found is None:
        return Unnamed(bare, _NOT_AN_OPERATION)
    verb = inbound.request.method
    # The proto says GET for SubscribeToTask and the prose says POST, so both are accepted here.
    allowed = verb == HTTP[found.method].verb or (
        found.method == "SubscribeToTask" and verb == "POST"
    )
    if not allowed:
        return Unnamed(bare, _NOT_AN_OPERATION)
    base: dict[str, JsonValue] = {} if found.id is None else {"id": found.id}
    if HTTP[found.method].where == "body" and verb == "POST":
        return await _body_params(inbound, bare, found.method, base)
    return Named(bare, found.method, {**_from_query(inbound.request), **base})


async def _body_params(
    inbound: Inbound, envelope: Envelope, method: Method, base: dict[str, JsonValue]
) -> Parsed:
    body = parse_json((await inbound.request.body()).decode(errors="replace"))
    if isinstance(body, Err):
        return Unnamed(envelope, body.error)
    if not isinstance(body.value, dict):
        return Unnamed(envelope, fault("InvalidRequestError", "the request body is a JSON object"))
    return Named(envelope, method, {**body.value, **base})


def _from_query(request: Request) -> dict[str, JsonValue]:
    params: dict[str, JsonValue] = {}
    for key, value in request.query_params.items():
        # Sent as a parameter as well as a header; it is the version check's, not the operation's.
        if key == VERSION_HEADER:
            continue
        if key in _NUMBERS:
            params[key] = int(value) if value.lstrip("-").isdigit() else value
        elif key in _BOOLEANS:
            params[key] = value == "true"
        else:
            params[key] = value
    return params
