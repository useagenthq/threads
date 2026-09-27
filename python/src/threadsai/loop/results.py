"""`tool_result` drafts, with L0 spill: a result whose text is over the
threshold keeps its full bytes as `ref` and shows the model a bounded preview."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from threadsai.log import ArtifactRef, CallId, ResultPart
from threadsai.loop.defaults import context
from threadsai.loop.drafts import ActorKind, draft
from threadsai.loop.runtime import Halt, Runtime, lost
from threadsai.loop.tools import Reference
from threadsai.redaction import redact_secrets
from threadsai.reduce.handlers import to_json
from threadsai.result import Err
from threadsai.store import Draft

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
    """Stores text as a durable artifact and returns its ref. Every text artifact the loop
    stores (a result, a commit, a child's output, a handoff transcript) is redacted here (C5)."""
    raw = redact_secrets(text).encode("utf-8")
    sha = await rt.store.put_artifact(raw)
    return {"sha256": sha, "bytes": len(raw), "media_type": "text/plain"}


async def close_call(
    rt: Runtime,
    call_id: CallId,
    origin: Literal["denied", "not_executed"],
    why: str,
    actor: ActorKind = "host",
) -> Halt | None:
    """Closes a call that never ran with an error result."""
    result = await result_draft(rt, call_id, why, As(origin, True, actor))
    done = await rt.append(result)
    return lost(done.error) if isinstance(done, Err) else None


def reference_drafts(references: Sequence[Reference]) -> list[Draft]:
    """One `injected` per recalled item or loaded skill, appended with the result that carries
    it; only a skill is trusted."""
    out: list[Draft] = []
    for r in references:
        origin: dict[str, JsonValue] = {"id": r.id, "version": r.version}
        if r.location is not None:
            origin["location"] = r.location
        data: dict[str, JsonValue] = {
            "source": r.source,
            "trust": "trusted_instruction" if r.source == "skill" else "untrusted_reference",
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
    """The result the model sees. Spilled bytes are a durable artifact before this draft, and
    redacted (C5): the writer redacts the event itself. `full` is output the source already
    spilled, and the text is then its bounded preview."""
    text = redact_secrets(text)
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
