"""`tool_result` drafts, with L0 spill: a result whose text is over the
threshold keeps its full bytes as `ref` and shows the model a bounded preview."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from threads.log import ArtifactRef, CallId, ResultPart
from threads.loop.defaults import context
from threads.loop.drafts import ActorKind, draft
from threads.loop.runtime import Runtime
from threads.loop.tools import Reference
from threads.reduce.handlers import to_json
from threads.store import Draft

type Origin = Literal[
    "executed", "materialized_from_commit", "denied", "not_executed", "interrupted", "deferred"
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


def reference_drafts(references: Sequence[Reference]) -> list[Draft]:
    """One untrusted `injected` per recalled item, appended with the result that carries it."""
    out: list[Draft] = []
    for r in references:
        origin: dict[str, JsonValue] = {"id": r.id, "version": r.version}
        if r.location is not None:
            origin["location"] = r.location
        data: dict[str, JsonValue] = {
            "source": r.source,
            "trust": "untrusted_reference",
            "origin": origin,
            "text": r.text,
        }
        out.append(draft("injected", data))
    return out


async def result_draft(  # noqa: PLR0913 - the result's parts ride with it
    rt: Runtime,
    call_id: CallId,
    text: str,
    how: As,
    full: ArtifactRef | None = None,
    *,
    content: Sequence[ResultPart] = (),
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
    if content:
        data["content"] = [to_json(p) for p in content]
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
