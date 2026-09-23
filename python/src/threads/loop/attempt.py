"""One model attempt: render, make the request durable,
then dispatch, then record what came back.

The Render v1 bytes are a durable artifact before the `model_request` that names them, and the
`model_request` is durable before the adapter sees the request. That order is the whole
persist-before-dispatch guarantee for model calls.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from threads.log import EventId, ModelRequestEvent, OutputPart, ToolUsePart
from threads.log.digest import sha256_hex
from threads.loop import budget, guard
from threads.loop.calls import call_drafts
from threads.loop.capabilities import mismatch
from threads.loop.drafts import draft
from threads.loop.history import open_cancel
from threads.loop.model import Done, Model, ModelRequest, ModelResponse, PartChunk, Rejected
from threads.loop.runtime import Failed, Runtime, WriterContext, epoch_model, fence, lost
from threads.redaction import SecretInProviderOutputError
from threads.reduce.handlers import to_json
from threads.result import Err
from threads.store import Draft, StoreError

type Purpose = Literal["turn", "compaction"]


async def request(
    rt: Runtime, attempt: int, purpose: Purpose = "turn", cause: EventId | None = None
) -> Failed | EventId | None:
    """Sends one attempt and records its outcome. Returns the model_request's event id, or None
    when the capability pre-check ended the turn before any model_request. `cause` is the
    compaction_requested a side request summarizes for: that side request never ends the turn,
    so a refusal before any request is `model_error` for its caller to answer."""
    compaction = purpose == "compaction"
    rendered = await rt.store.render(rt.events, compaction=compaction, cause=cause)
    if isinstance(rendered, Err):
        code = rendered.error.code
        if code in ("artifact_missing", "artifact_corrupt"):
            return Failed(code, rendered.error.message)
        raise AssertionError(f"the next request can't render: {rendered.error}")
    body, line0 = rendered.value.body, rendered.value.line0
    model = epoch_model(rt)
    if model is None:
        return Failed("model_error", "no adapter for this settings epoch's model")
    refused = await _unsupported(rt, body, model, cause)
    if refused is not False:
        return refused
    refused = await budget.reserve(rt, _answer(compaction, cause))
    if refused is not None or not rt.fold.in_turn:
        return refused
    sha = await rt.store.put_artifact(body)
    data: dict[str, JsonValue] = {
        "attempt": attempt,
        "declared_prefix": {"bytes": len(line0), "sha256": sha256_hex(line0)},
        "request_ref": {"sha256": sha, "bytes": len(body), "media_type": "application/x-ndjson"},
    }
    if compaction:
        data["purpose"] = "compaction"
    if cause is not None:
        data["cause_event_id"] = cause
    appended = await rt.append(draft("model_request", data))
    if isinstance(appended, Err):
        return lost(appended.error)
    event = appended.value[0]
    if not isinstance(event, ModelRequestEvent):
        raise AssertionError("a model_request draft stored another type")
    sent = await _dispatch(rt, model, event, body, cause)
    await budget.settle(rt)
    return sent


async def _unsupported(
    rt: Runtime, body: bytes, model: Model, cause: EventId | None
) -> Failed | Literal[False] | None:
    """The capability pre-check: False when the model can take the request. Otherwise the turn
    ends error, or for a requested compaction's side request its caller answers model_error."""
    unsupported = mismatch(body, model.info)
    if unsupported is None:
        return False
    if cause is not None:
        return Failed("model_error", f"the model can't take this request: {unsupported}")
    ended = await rt.append(draft("turn_completed", {"reason": "error", "code": unsupported}))
    return lost(ended.error) if isinstance(ended, Err) else None


def _answer(compaction: bool, cause: EventId | None) -> Draft | None:
    """A side request's compaction_failed, recorded with a budget refusal."""
    if not compaction:
        return None
    data: dict[str, JsonValue] = {"stage": "summary", "reason": "model_error"}
    if cause is not None:
        data["cause_event_id"] = cause
    return draft("compaction_failed", data)


async def _dispatch(
    rt: Runtime, model: Model, event: ModelRequestEvent, body: bytes, cause: EventId | None
) -> Failed | EventId | None:
    """Sends the durable request and records its outcome in one batch."""
    guard.check(model)
    stale = await fence(rt)
    if stale is not None:
        return stale
    req = ModelRequest(f"{rt.writer.branch_id}:{event.event_id}", body)
    outcome = await _collect(rt, model, req)
    if isinstance(outcome, Rejected) and outcome.reason == "stale_epoch":
        # The adapter's fence refused at its send point: this writer can't append anything.
        return Failed("branch_busy", "the lease moved before the send")
    recorded = await rt.append(*outcome_drafts(rt, event.event_id, outcome, cause=cause))
    if isinstance(recorded, Err):
        return lost(recorded.error)
    return None if cause is None and _refused(rt, outcome) else event.event_id


@dataclass(frozen=True, slots=True)
class Leaked:
    """The response held a registered secret in provider material that can't be redacted
    (C5): nothing of it is stored, and the turn ends with secret_in_provider_output."""


type Outcome = ModelResponse | Rejected | Leaked | None
"""A response, a rejection before any content, a refused leak, or None when the outcome is
unknown."""


async def _collect(rt: Runtime, model: Model, req: ModelRequest) -> Outcome:
    parts: list[OutputPart] = []
    try:
        async for chunk in model.send(req, WriterContext(rt)):
            match chunk:
                case PartChunk(part=part):
                    parts.append(part)
                case Done(stop_reason=stop, usage=usage):
                    return ModelResponse(tuple(parts), stop, usage, None)
                case Rejected():
                    return chunk
                case _:
                    pass
    except (AssertionError, StoreError):
        # A broken invariant, or the store's outage: the request stays open, for recovery.
        raise
    except SecretInProviderOutputError:
        return Leaked()
    except Exception:
        # After dispatch every adapter failure is uncertainty, never a plain error.
        return None
    return None


def outcome_drafts(
    rt: Runtime, request_id: EventId, outcome: Outcome, *, cause: EventId | None = None
) -> Sequence[Draft]:
    """The events one attempt's outcome appends, in one batch: the response with its tool calls,
    or the abandonment. A send-time refusal ends the turn unless the attempt is a requested
    compaction's side request (`cause`), which its caller answers. A refused leak answers
    `cause` in the same batch and ends the turn, unless a cancel is already requested: then the
    cancellation step closes the turn."""
    ends_turn = cause is None
    match outcome:
        case ModelResponse():
            return response_drafts(rt, request_id, outcome)
        case Rejected(
            reason="content_unsupported"
            | "continuation_unsupported"
            | "transport_fence_unsupported" as code
        ):
            data = {
                "request_event_id": request_id,
                "provider_outcome": "not_sent",
                "reason": "provider_error",
                "billing": "not_billed",
            }
            ended = {"reason": "error", "code": code}
            abandoned = draft("model_attempt_abandoned", data)
            return [abandoned, draft("turn_completed", ended)] if ends_turn else [abandoned]
        case Rejected():
            return [draft("model_attempt_abandoned", _rejection(request_id, outcome))]
        case Leaked():
            # The response arrived (it may be billed) but none of it is kept.
            data = {
                "request_event_id": request_id,
                "provider_outcome": "unknown",
                "reason": "provider_error",
            }
            ended = {"reason": "error", "code": "secret_in_provider_output"}
            answered = [] if cause is None else [_leak_answer(request_id, cause)]
            closing = [draft("turn_completed", ended)] if _leak_ends_turn(rt) else []
            return [draft("model_attempt_abandoned", data), *answered, *closing]
        case None:
            data: dict[str, JsonValue] = {
                "request_event_id": request_id,
                "provider_outcome": "unknown",
                "reason": "stream_broken",
            }
            return [draft("model_attempt_abandoned", data)]


def _leak_answer(request_id: EventId, cause: EventId) -> Draft:
    """The requested compaction's outcome when its summary was refused as a leak."""
    data: dict[str, JsonValue] = {
        "stage": "summary",
        "reason": "model_error",
        "request_event_id": request_id,
        "cause_event_id": cause,
    }
    return draft("compaction_failed", data)


def _leak_ends_turn(rt: Runtime) -> bool:
    """A refused leak ends the turn itself, unless a cancel already did: the cancel is then
    processed, and the turn ends cancelled."""
    return open_cancel(rt.events) is None


def _refused(rt: Runtime, outcome: Outcome) -> bool:
    """A send-time refusal or a refused leak already ended the turn with its code."""
    return (isinstance(outcome, Leaked) and _leak_ends_turn(rt)) or (
        isinstance(outcome, Rejected)
        and outcome.reason
        in (
            "content_unsupported",
            "continuation_unsupported",
            "transport_fence_unsupported",
        )
    )


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
