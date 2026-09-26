"""The A2A error set with its JSON-RPC code and HTTP status, taken from the pinned
specification's error-code mapping table (spec/schema/a2a/README.md repeats it), never from
memory. One table serves both bindings and both directions: we answer with it, and we read a
peer's error by code."""

from dataclasses import dataclass
from typing import Final, Literal

type A2aErrorName = Literal[
    "JSONParseError",
    "InvalidRequestError",
    "MethodNotFoundError",
    "InvalidParamsError",
    "InternalError",
    "TaskNotFoundError",
    "TaskNotCancelableError",
    "PushNotificationNotSupportedError",
    "UnsupportedOperationError",
    "ContentTypeNotSupportedError",
    "InvalidAgentResponseError",
    "ExtendedAgentCardNotConfiguredError",
    "ExtensionSupportRequiredError",
    "VersionNotSupportedError",
]


@dataclass(frozen=True, slots=True)
class Coded:
    code: int
    status: int


A2A_ERRORS: Final[dict[A2aErrorName, Coded]] = {
    "JSONParseError": Coded(-32700, 400),
    "InvalidRequestError": Coded(-32600, 400),
    "MethodNotFoundError": Coded(-32601, 404),
    "InvalidParamsError": Coded(-32602, 400),
    "InternalError": Coded(-32603, 500),
    "TaskNotFoundError": Coded(-32001, 404),
    "TaskNotCancelableError": Coded(-32002, 400),
    "PushNotificationNotSupportedError": Coded(-32003, 400),
    "UnsupportedOperationError": Coded(-32004, 400),
    "ContentTypeNotSupportedError": Coded(-32005, 400),
    "InvalidAgentResponseError": Coded(-32006, 500),
    "ExtendedAgentCardNotConfiguredError": Coded(-32007, 400),
    "ExtensionSupportRequiredError": Coded(-32008, 400),
    "VersionNotSupportedError": Coded(-32009, 400),
}

A2A_ERROR_NAMES: Final[tuple[A2aErrorName, ...]] = tuple(A2A_ERRORS)


@dataclass(frozen=True, slots=True)
class A2aFault:
    """A peer's error, or ours: an answer, never a doubt."""

    name: A2aErrorName
    message: str


def fault(name: A2aErrorName, message: str) -> A2aFault:
    return A2aFault(name, message)


def json_rpc_code(name: A2aErrorName) -> int:
    return A2A_ERRORS[name].code


def http_status(name: A2aErrorName) -> int:
    return A2A_ERRORS[name].status


_BY_CODE: Final[dict[int, A2aErrorName]] = {e.code: n for n, e in A2A_ERRORS.items()}


def error_by_code(code: int) -> A2aErrorName | None:
    """A peer's JSON-RPC code as its A2A name, or None for a code outside the table."""
    return _BY_CODE.get(code)
