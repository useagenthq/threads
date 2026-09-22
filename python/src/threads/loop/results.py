"""`tool_result` drafts, with L0 spill: a result whose text is over the
threshold keeps its full bytes as `ref` and shows the model a bounded preview."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import CallId, Spill
from threads.loop.drafts import ActorKind, draft
from threads.loop.runtime import Runtime
from threads.reduce.fold import policy
from threads.store import Draft

if TYPE_CHECKING:
    from pydantic import JsonValue

type Origin = Literal[
    "executed", "materialized_from_commit", "denied", "not_executed", "interrupted"
]

_DEFAULT_SPILL = Spill(
    threshold_bytes=32_768, head_bytes=2048, tail_bytes=1024, request_budget_bytes=204_800
)
_MARKER = (
    '\n[output truncated: {n} bytes; read_tool_result(call_id="{id}", offset, length) '
    "returns the rest]\n"
)


def _spill(rt: Runtime) -> Spill:
    pinned = policy(rt.fold)
    if pinned is None or pinned.context is MISSING:
        return _DEFAULT_SPILL
    return pinned.context.spill


@dataclass(frozen=True, slots=True)
class As:
    """How a result is recorded: its origin, whether it is an error, and who writes it."""

    origin: Origin
    is_error: bool = False
    actor: ActorKind = "tool"


async def result_draft(rt: Runtime, call_id: CallId, text: str, how: As) -> Draft:
    """The result the model sees. Spilled bytes are a durable artifact before this draft."""
    data: dict[str, JsonValue] = {
        "call_id": call_id,
        "completeness": "complete",
        "is_error": how.is_error,
        "origin": how.origin,
        "preview": text,
    }
    raw = text.encode("utf-8")
    spill = _spill(rt)
    if len(raw) > spill.threshold_bytes:
        sha = await rt.store.put_artifact(raw)
        head = raw[: spill.head_bytes].decode("utf-8", "ignore")
        tail = raw[len(raw) - spill.tail_bytes :].decode("utf-8", "ignore")
        data["preview"] = head + _MARKER.format(n=len(raw), id=call_id) + tail
        data["ref"] = {"sha256": sha, "bytes": len(raw), "media_type": "text/plain"}
    return draft("tool_result", data, how.actor)
