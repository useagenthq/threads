"""The operations we deliberately do not serve. Each answers by name rather than as an unknown
method, because the card says so: a client that reads `pushNotifications: false` and then calls a
config operation anyway deserves the specific refusal, not a 404 that looks like a wrong path."""

import re
from typing import Final

from threads.a2a.protocol import A2aFault, fault

_PUSH_CONFIG: Final[frozenset[str]] = frozenset(
    {
        "CreateTaskPushNotificationConfig",
        "GetTaskPushNotificationConfig",
        "ListTaskPushNotificationConfigs",
        "DeleteTaskPushNotificationConfig",
    }
)
"""Push notifications are not supported, so every config operation says exactly that."""

_EXTENDED_CARD: Final = "GetExtendedAgentCard"

_PUSH_PATH: Final = re.compile(r"^/tasks/[^/]+/pushNotificationConfigs(/[^/]+)?$")

_NO_PUSH: Final = (
    "this agent does not support push notifications; "
    "follow the task with SubscribeToTask or GetTask"
)
_NO_EXTENDED: Final = (
    "this agent serves no extended card; its public card is at /.well-known/agent-card.json"
)


def cut_operation(method: str) -> A2aFault | None:
    """The refusal an operation we do not serve earns, or None when it is not one of them."""
    if method in _PUSH_CONFIG:
        return fault("PushNotificationNotSupportedError", _NO_PUSH)
    if method == _EXTENDED_CARD:
        return fault("UnsupportedOperationError", _NO_EXTENDED)
    return None


def cut_path(path: str, verb: str) -> A2aFault | None:
    """The HTTP+JSON paths of those operations, so they answer the same way in both bindings.
    Matched before the served table, since none of these paths is one of ours."""
    if path == "/extendedAgentCard":
        return cut_operation(_EXTENDED_CARD if verb == "GET" else "")
    if _PUSH_PATH.match(path) is not None:
        return cut_operation("GetTaskPushNotificationConfig")
    return None
