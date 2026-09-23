"""`tool_result` drafts, with L0 spill: a result whose text is over the
threshold keeps its full bytes as `ref` and shows the model a bounded preview."""

from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from threads.log import ArtifactRef, CallId
from threads.loop.defaults import context
from threads.loop.drafts import ActorKind, draft
from threads.loop.runtime import Runtime
from threads.reduce.handlers import to_json
from threads.store import Draft

type Origin = Literal[
    "executed", "materialized_from_commit", "denied", "not_executed", "interrupted"
]

_MARKER = (
    '\n[output truncated: {n} bytes; read_tool_result(call_id="{id}", offset, length) '
    "returns the rest]\n"
)


@dataclass(frozen=True, slots=True)
class As:
    """How a result is recorded: its origin, whether it is an error, and who writes it."""

    origin: Origin
    is_error: bool = False
    actor: ActorKind = "tool"


async def text_ref(rt: Runtime, text: str) -> JsonValue:
    """Stores text as a durable artifact and returns its ref."""
    raw = text.encode("utf-8")
    sha = await rt.store.put_artifact(raw)
    return {"sha256": sha, "bytes": len(raw), "media_type": "text/plain"}


async def result_draft(
    rt: Runtime, call_id: CallId, text: str, how: As, full: ArtifactRef | None = None
) -> Draft:
    """The result the model sees. Spilled bytes are a durable artifact before this draft;
    `full` is output the source already spilled, and the text is then its bounded preview."""
    data: dict[str, JsonValue] = {
        "call_id": call_id,
        "completeness": "complete",
        "is_error": how.is_error,
        "origin": how.origin,
        "preview": text,
    }
    raw = text.encode("utf-8")
    spill = context(rt.fold).spill
    if full is not None:
        data["ref"] = to_json(full)
    elif len(raw) > spill.threshold_bytes:
        head = raw[: spill.head_bytes].decode("utf-8", "ignore")
        tail = raw[len(raw) - spill.tail_bytes :].decode("utf-8", "ignore")
        data["preview"] = head + _MARKER.format(n=len(raw), id=call_id) + tail
        data["ref"] = await text_ref(rt, text)
    return draft("tool_result", data, how.actor)
