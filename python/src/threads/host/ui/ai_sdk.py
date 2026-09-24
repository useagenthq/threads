"""A committed event as AI SDK UI message stream v1 chunks (spec/schema/ui/README.md,
"Mapping"). Pure: the event and the facts of the run's events before it, nothing else."""

from typing import TYPE_CHECKING

from pydantic.experimental.missing_sentinel import MISSING

from threads.host.ui.facts import RunFacts
from threads.host.ui.frame import Chunk
from threads.host.ui.framed import Called, Shown, part_id, shown_parts
from threads.log import (
    AgentFinishedEvent,
    AgentSpawnedEvent,
    ApprovalDeniedEvent,
    ApprovalGrantedEvent,
    ApprovalRequestedEvent,
    Event,
    ModelAttemptAbandonedEvent,
    ModelRequestEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    RetryScheduledEvent,
    ToolResultEvent,
    ToolResultLateEvent,
)

if TYPE_CHECKING:
    from pydantic import JsonValue

type _Model = (
    ModelRequestEvent
    | ModelResponseEvent
    | ModelResponseRecoveredEvent
    | ModelAttemptAbandonedEvent
)
type _Approval = ApprovalRequestedEvent | ApprovalGrantedEvent | ApprovalDeniedEvent


def ai_sdk_chunks(e: Event, facts: RunFacts) -> list[Chunk]:
    match e:
        case (
            ModelRequestEvent()
            | ModelResponseEvent()
            | ModelResponseRecoveredEvent()
            | ModelAttemptAbandonedEvent()
        ):
            return _model(e, facts)
        case ApprovalRequestedEvent() | ApprovalGrantedEvent() | ApprovalDeniedEvent():
            return [_approval(e)] if facts.proposed(e.data.call_id) else []
        case ToolResultEvent() | ToolResultLateEvent():
            return [_result(e)] if facts.proposed(e.data.call_id) else []
        case AgentSpawnedEvent() | AgentFinishedEvent():
            return _child(e, facts)
        case RetryScheduledEvent(data=data):
            status: JsonValue = {"status": "retry_wait", "until": data.not_before}
            return [{"type": "data-status", "id": "status", "data": status}]
        case _:
            return []


def _child(e: AgentSpawnedEvent | AgentFinishedEvent, facts: RunFacts) -> list[Chunk]:
    if isinstance(e, AgentSpawnedEvent):
        return [subagent(e.data.child_thread_id, e.data.agent_name, "running")]
    spawned = facts.spawned(e.data.child_thread_id)
    return [] if spawned is None else [subagent(spawned.child, spawned.agent, e.data.status)]


def _model(e: _Model, facts: RunFacts) -> list[Chunk]:
    """A turn's model step: a compaction side request and its answer show nothing."""
    if isinstance(e, ModelRequestEvent):
        return [] if e.data.purpose == "compaction" else [{"type": "start-step"}]
    request = e.data.request_event_id
    if not facts.is_turn(request):
        return []
    if isinstance(e, ModelAttemptAbandonedEvent):
        data: JsonValue = {"status": "abandoned", "reason": e.data.reason}
        return [{"type": "data-attempt", "id": request, "data": data}, {"type": "finish-step"}]
    return [*(c for p in shown_parts(e) for c in part(request, p)), {"type": "finish-step"}]


def part(request_id: str, p: Shown) -> list[Chunk]:
    """One response part: text and reasoning open, fill and close a part; a call is one chunk."""
    if isinstance(p, Called):
        return [
            {
                "type": "tool-input-available",
                "toolCallId": p.call_id,
                "toolName": p.name,
                "input": dict(p.input),
            }
        ]
    pid = part_id(request_id, p.index)
    return [
        {"type": f"{p.kind}-start", "id": pid},
        {"type": f"{p.kind}-delta", "id": pid, "delta": p.text},
        {"type": f"{p.kind}-end", "id": pid},
    ]


def _approval(e: _Approval) -> Chunk:
    if isinstance(e, ApprovalRequestedEvent):
        return {
            "type": "tool-approval-request",
            "approvalId": e.data.challenge_id,
            "toolCallId": e.data.call_id,
        }
    chunk: dict[str, JsonValue] = {
        "type": "tool-approval-response",
        "approvalId": e.data.challenge_id,
        "approved": isinstance(e, ApprovalGrantedEvent),
    }
    if e.data.reason is not MISSING:
        chunk["reason"] = e.data.reason
    return chunk


def _result(e: ToolResultEvent | ToolResultLateEvent) -> Chunk:
    call, preview = e.data.call_id, e.data.preview
    origin = e.data.origin if isinstance(e, ToolResultEvent) else None
    if origin == "deferred":
        return {
            "type": "tool-output-available",
            "toolCallId": call,
            "output": preview,
            "preliminary": True,
        }
    if origin == "denied":
        return {"type": "tool-output-denied", "toolCallId": call}
    if e.data.is_error:
        return {"type": "tool-output-error", "toolCallId": call, "errorText": preview}
    return {"type": "tool-output-available", "toolCallId": call, "output": preview}


def subagent(child: str, agent: str, status: str) -> Chunk:
    """A legacy subagent as one data part, replaced in place by id as its status changes."""
    data: JsonValue = {"agent": agent, "status": status}
    return {"type": "data-subagent", "id": child, "data": data}
