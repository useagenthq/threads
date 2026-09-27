"""The one request body a call ever has.

It is built once, stored once, and re-sent unchanged: a re-serialization that differed by one byte
would defeat a peer's deduplication while looking correct, so the bytes are the record and this
module runs only on a call's first attempt."""

from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue

from threadsai.a2a.protocol import Wire, outbound

HISTORY: Final = 100
"""Enough history for our own message to be visible when the partner keeps any at all."""


@dataclass(frozen=True, slots=True)
class Building:
    wire: Wire
    message_id: str
    context_id: str
    task_id: str | None
    text: str
    metadata: dict[str, JsonValue] | None


@dataclass(frozen=True, slots=True)
class Built:
    body: str
    message_id: str
    context_id: str


def request_body(b: Building) -> Built:
    message: dict[str, JsonValue] = {
        "messageId": b.message_id,
        "contextId": b.context_id,
        "role": "ROLE_USER",
        "parts": [{"text": b.text}],
    }
    if b.task_id is not None:
        message["taskId"] = b.task_id
    if b.metadata is not None:
        message["metadata"] = b.metadata
    params: dict[str, JsonValue] = {
        "message": message,
        # The first response is the only one whose loss matters, so it is small and fast.
        "configuration": {"returnImmediately": True},
    }
    # The envelope id is the derived messageId: unique per call, and derived rather than random so
    # the bytes this module stores are the same on every build of the same call.
    built = outbound(b.wire, "SendMessage", params, b.message_id)
    if built.body is None:
        raise AssertionError("SendMessage carries its request in the body")
    return Built(built.body, b.message_id, b.context_id)


def list_params(context_id: str, page_size: int) -> dict[str, JsonValue]:
    """`ListTasks` for one context: how reconciliation asks a partner what it already has.

    One page only: a partner that truncates history never proves absence anyway, so a second page
    would add latency to an answer that still cannot settle a park."""
    return {"contextId": context_id, "pageSize": page_size, "historyLength": HISTORY}
