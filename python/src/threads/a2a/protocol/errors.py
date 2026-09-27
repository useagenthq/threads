"""The A2A error set with its JSON-RPC code, HTTP status and `google.rpc.ErrorInfo` reason, taken
from the pinned specification's error-code mapping table (spec/schema/a2a/README.md repeats it),
never from memory. One table serves both bindings and both directions: we answer with it, and we
read a peer's error by code, by status and by reason."""

from dataclasses import dataclass
from typing import Final, Literal

from pydantic import JsonValue, ValidationError

from threads._generated.a2a_v1 import ErrorInfo

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
    reason: str
    """The `google.rpc.ErrorInfo` reason, which names this error the same way on either binding."""


A2A_ERRORS: Final[dict[A2aErrorName, Coded]] = {
    "JSONParseError": Coded(-32700, 400, "JSON_PARSE"),
    "InvalidRequestError": Coded(-32600, 400, "INVALID_REQUEST"),
    "MethodNotFoundError": Coded(-32601, 404, "METHOD_NOT_FOUND"),
    "InvalidParamsError": Coded(-32602, 400, "INVALID_PARAMS"),
    "InternalError": Coded(-32603, 500, "INTERNAL"),
    "TaskNotFoundError": Coded(-32001, 404, "TASK_NOT_FOUND"),
    "TaskNotCancelableError": Coded(-32002, 400, "TASK_NOT_CANCELABLE"),
    "PushNotificationNotSupportedError": Coded(-32003, 400, "PUSH_NOTIFICATION_NOT_SUPPORTED"),
    "UnsupportedOperationError": Coded(-32004, 400, "UNSUPPORTED_OPERATION"),
    "ContentTypeNotSupportedError": Coded(-32005, 400, "CONTENT_TYPE_NOT_SUPPORTED"),
    "InvalidAgentResponseError": Coded(-32006, 500, "INVALID_AGENT_RESPONSE"),
    "ExtendedAgentCardNotConfiguredError": Coded(-32007, 400, "EXTENDED_AGENT_CARD_NOT_CONFIGURED"),
    "ExtensionSupportRequiredError": Coded(-32008, 400, "EXTENSION_SUPPORT_REQUIRED"),
    "VersionNotSupportedError": Coded(-32009, 400, "VERSION_NOT_SUPPORTED"),
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


def error_reason(name: A2aErrorName) -> str:
    """The ErrorInfo reason this error is named by, which is stable across bindings."""
    return A2A_ERRORS[name].reason


_BY_REASON: Final[dict[str, A2aErrorName]] = {e.reason: n for n, e in A2A_ERRORS.items()}


def error_by_reason(reason: str) -> A2aErrorName | None:
    """A peer's ErrorInfo reason as its A2A name, or None for a reason outside the table."""
    return _BY_REASON.get(reason)


A2A_ERROR_DOMAIN: Final = "a2a.dev"
"""The domain our own ErrorInfo carries. `domain` scopes `reason`, so it is the protocol's domain
rather than this host's: two A2A implementations must name the same error the same way. Inbound we
read any domain and check only the reason, because a partner's domain is theirs to choose."""

ERROR_INFO_TYPE: Final = "type.googleapis.com/google.rpc.ErrorInfo"
"""The `@type` a `google.rpc.Status.details` entry carries when it is an ErrorInfo."""


def error_info(name: A2aErrorName) -> dict[str, JsonValue]:
    """Our own ErrorInfo for one fault, as the JSON it goes out as on either binding."""
    return {"@type": ERROR_INFO_TYPE, "reason": error_reason(name), "domain": A2A_ERROR_DOMAIN}


@dataclass(frozen=True, slots=True)
class FoundErrorInfo:
    info: ErrorInfo
    named: A2aErrorName | None
    """The A2A error the reason names, or None for a reason outside the table."""


def error_info_in(value: JsonValue) -> FoundErrorInfo | None:
    """The ErrorInfo a value carries, and the A2A error its reason names. The two bindings put it
    somewhere different — HTTP+JSON in the `details` list of a `google.rpc.Status`, JSON-RPC in the
    error's own `data` — so a list is scanned and anything else is tried as the ErrorInfo itself.
    None means there was none, which is the HTTP+JSON binding's one refusable case."""
    entries = value if isinstance(value, list) else [value]
    for entry in entries:
        try:
            info = ErrorInfo.model_validate(entry)
        except (ValidationError, ValueError):
            continue
        return FoundErrorInfo(info, error_by_reason(info.reason))
    return None
