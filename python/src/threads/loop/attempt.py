"""One model attempt: render, make the request durable,
then dispatch, then record what came back.

The Render v1 bytes are a durable artifact before the `model_request` that names them, and the
`model_request` is durable before the adapter sees the request. That order is the whole
persist-before-dispatch guarantee for model calls.
"""

from collections.abc import Sequence
from typing import Literal

from pydantic import JsonValue

from threads.log import EventId, ModelRequestEvent, OutputPart, ToolUsePart
from threads.log.digest import sha256_hex
from threads.loop import guard
from threads.loop.calls import call_drafts
from threads.loop.drafts import draft
from threads.loop.model import Done, ModelRequest, ModelResponse, PartChunk, Rejected
from threads.loop.runtime import Failed, Runtime, fence, lost
from threads.reduce.handlers import to_json
from threads.result import Err
from threads.store import Draft

type Purpose = Literal["turn", "compaction"]


async def request(rt: Runtime, attempt: int, purpose: Purpose = "turn") -> Failed | EventId:
    """Sends one attempt and records its outcome. Returns the model_request's event id."""
    rendered = await rt.store.render(rt.events, compaction=purpose == "compaction")
    if isinstance(rendered, Err):
        code = rendered.error.code
        if code in ("artifact_missing", "artifact_corrupt"):
            return Failed(code, rendered.error.message)
        raise AssertionError(f"the next request can't render: {rendered.error}")
    body, line0 = rendered.value.body, rendered.value.line0
    sha = await rt.store.put_artifact(body)
    data: dict[str, JsonValue] = {
        "attempt": attempt,
        "declared_prefix": {"bytes": len(line0), "sha256": sha256_hex(line0)},
        "request_ref": {"sha256": sha, "bytes": len(body), "media_type": "application/x-ndjson"},
    }
    if purpose == "compaction":
        data["purpose"] = "compaction"
    appended = await rt.append(draft("model_request", data))
    if isinstance(appended, Err):
        return lost(appended.error)
    event = appended.value[0]
    if not isinstance(event, ModelRequestEvent):
        raise AssertionError("a model_request draft stored another type")
    guard.check(rt.model)
    stale = await fence(rt)
    if stale is not None:
        return stale
    outcome = await _collect(rt, ModelRequest(f"{rt.writer.branch_id}:{event.event_id}", body))
    recorded = await rt.append(*outcome_drafts(rt, event.event_id, outcome))
    return lost(recorded.error) if isinstance(recorded, Err) else event.event_id


type Outcome = ModelResponse | Rejected | None
"""A response, a rejection before any content, or None when the outcome is unknown."""


async def _collect(rt: Runtime, req: ModelRequest) -> Outcome:
    parts: list[OutputPart] = []
    try:
        async for chunk in rt.model.send(req):
            match chunk:
                case PartChunk(part=part):
                    parts.append(part)
                case Done(stop_reason=stop, usage=usage):
                    return ModelResponse(tuple(parts), stop, usage, None)
                case Rejected():
                    return chunk
                case _:
                    pass
    except AssertionError:
        raise
    except Exception:
        # After dispatch every adapter failure is uncertainty, never a plain error.
        return None
    return None


def outcome_drafts(rt: Runtime, request_id: EventId, outcome: Outcome) -> Sequence[Draft]:
    """The events one attempt's outcome appends, in one batch: the response with its tool calls,
    or the abandonment."""
    match outcome:
        case ModelResponse():
            return response_drafts(rt, request_id, outcome)
        case Rejected():
            return [draft("model_attempt_abandoned", _rejection(request_id, outcome))]
        case None:
            data: dict[str, JsonValue] = {
                "request_event_id": request_id,
                "provider_outcome": "unknown",
                "reason": "stream_broken",
            }
            return [draft("model_attempt_abandoned", data)]


def _rejection(request_id: EventId, rejected: Rejected) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {
        "request_event_id": request_id,
        "provider_outcome": "failed",
        "reason": rejected.reason,
    }
    if rejected.http_status is not None:
        data["http_status"] = rejected.http_status
    if rejected.retry_after_ms is not None:
        data["retry_after_ms"] = rejected.retry_after_ms
    if rejected.billing is not None:
        data["billing"] = rejected.billing
    return data


def response_drafts(
    rt: Runtime, request_id: EventId, response: ModelResponse, recovered_as: str | None = None
) -> Sequence[Draft]:
    """The response and, in the same batch, a `tool_call` per complete `tool_use` part. A call a
    max_tokens stop cut off never exists: adapters emit only complete parts.
    `recovered_as` is the provider request id of a response recovery found by lookup."""
    data: dict[str, JsonValue] = {
        "request_event_id": request_id,
        "content": [to_json(p) for p in response.content],
        "stop_reason": response.stop_reason,
        "usage": to_json(response.usage),
        "completeness": "complete",
    }
    first = draft("model_response", data, "model")
    if recovered_as is not None:
        data["provider_request_id"] = recovered_as
        first = draft("model_response_recovered", data, "recovery")
    uses = [p for p in response.content if isinstance(p, ToolUsePart)]
    return [first, *call_drafts(rt, request_id, uses)]
