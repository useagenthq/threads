"""Event drafts the loop appends: the schema pins each known type's `critical` flag, so it is
read from the generated models, never restated here."""

from collections.abc import Mapping
from typing import Final, Literal, get_args

from pydantic import JsonValue

from threads.log import Event, EventId, ToolUsePart
from threads.store import Draft

type ActorKind = Literal["host", "model", "tool", "recovery"]


def _critical_flags() -> Mapping[str, bool]:
    flags: dict[str, bool] = {}
    union = get_args(Event.__value__)[0]
    for model in get_args(union):
        (tag,) = get_args(model.model_fields["type"].annotation)
        (critical,) = get_args(model.model_fields["critical"].annotation)
        flags[str(tag)] = critical is True
    return flags


_CRITICAL: Final = _critical_flags()


def draft(kind: str, data: Mapping[str, JsonValue], actor: ActorKind = "host") -> Draft:
    """A draft of a known event type, by an actor that needs no principal."""
    return Draft(kind, data, {"kind": actor}, _CRITICAL[kind])


def call_draft(request_id: EventId, use: ToolUsePart) -> Draft:
    """A response's `tool_use` part as its `tool_call`."""
    data: dict[str, JsonValue] = {
        "call_id": use.call_id,
        "name": use.name,
        "input": dict(use.input),
        "request_event_id": request_id,
    }
    return draft("tool_call", data)
