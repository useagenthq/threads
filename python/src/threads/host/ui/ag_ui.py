"""A committed event as AG-UI 1.0 events (spec/schema/ui/README.md, "Mapping"). Pure: the event
and the facts of the run's events before it, nothing else."""

from typing import TYPE_CHECKING

from threads.host.ui.facts import RunFacts, Spawned
from threads.host.ui.frame import Chunk
from threads.host.ui.framed import Called, Shown, part_id, shown_parts
from threads.log import (
    AgentFinishedEvent,
    AgentSpawnedEvent,
    Event,
    ModelAttemptAbandonedEvent,
    ModelRequestEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    RetryScheduledEvent,
    ToolResultEvent,
    ToolResultLateEvent,
)
from threads.log.jcs import canonicalize
from threads.result import Ok

if TYPE_CHECKING:
    from pydantic import JsonValue

MODEL_STEP: Chunk = {"type": "STEP_STARTED", "stepName": "model"}
_STEP_DONE: Chunk = {"type": "STEP_FINISHED", "stepName": "model"}

type _Model = (
    ModelRequestEvent
    | ModelResponseEvent
    | ModelResponseRecoveredEvent
    | ModelAttemptAbandonedEvent
)


def ag_ui_events(e: Event, facts: RunFacts) -> list[Chunk]:
    match e:
        case (
            ModelRequestEvent()
            | ModelResponseEvent()
            | ModelResponseRecoveredEvent()
            | ModelAttemptAbandonedEvent()
        ):
            return _model(e, facts)
        case ToolResultEvent(data=data) if data.origin == "deferred":
            return []
        case ToolResultEvent() | ToolResultLateEvent():
            return [_result(e)] if facts.proposed(e.data.call_id) else []
        case AgentSpawnedEvent() | AgentFinishedEvent():
            return _child(e, facts)
        case RetryScheduledEvent(data=data):
            value: JsonValue = {"until": data.not_before}
            return [{"type": "CUSTOM", "name": "threads.retry_wait", "value": value}]
        case _:
            # An approval becomes an interrupt when the run parks on it.
            return []


def _model(e: _Model, facts: RunFacts) -> list[Chunk]:
    """A turn's model step: a compaction side request and its answer show nothing."""
    if isinstance(e, ModelRequestEvent):
        return [] if e.data.purpose == "compaction" else [MODEL_STEP]
    request = e.data.request_event_id
    if not facts.is_turn(request):
        return []
    if isinstance(e, ModelAttemptAbandonedEvent):
        value: JsonValue = {"requestId": request, "reason": e.data.reason}
        return [
            {"type": "CUSTOM", "name": "threads.attempt_abandoned", "value": value},
            _STEP_DONE,
        ]
    return [*(c for p in shown_parts(e) for c in part(request, p)), _STEP_DONE]


def _child(e: AgentSpawnedEvent | AgentFinishedEvent, facts: RunFacts) -> list[Chunk]:
    if isinstance(e, AgentSpawnedEvent):
        return [started(Spawned(e.data.child_thread_id, e.data.agent_name, e.data.call_id))]
    return _finished(e, facts)


def _finished(e: AgentFinishedEvent, facts: RunFacts) -> list[Chunk]:
    spawned = facts.spawned(e.data.child_thread_id)
    if spawned is None:
        return []
    if e.data.status == "completed":
        outcome: JsonValue = {"type": "success"}
        return [{"type": "SUBAGENT_FINISHED", "subagentRunId": spawned.child, "outcome": outcome}]
    return [
        {
            "type": "SUBAGENT_ERROR",
            "subagentRunId": spawned.child,
            "message": f"subagent {spawned.agent} ended: {e.data.status}",
            "code": e.data.status,
        }
    ]


def part(request_id: str, p: Shown) -> list[Chunk]:
    """One response part: text and reasoning are messages of their own; calls join the request's
    assistant message."""
    if isinstance(p, Called):
        args = canonicalize(dict(p.input))
        if not isinstance(args, Ok):
            raise AssertionError(f"a logged tool input is JSON: {args.error}")
        return [
            {
                "type": "TOOL_CALL_START",
                "toolCallId": p.call_id,
                "toolCallName": p.name,
                "parentMessageId": request_id,
            },
            {"type": "TOOL_CALL_ARGS", "toolCallId": p.call_id, "delta": args.value},
            {"type": "TOOL_CALL_END", "toolCallId": p.call_id},
        ]
    mid = part_id(request_id, p.index)
    if p.kind == "text":
        return [
            {"type": "TEXT_MESSAGE_START", "messageId": mid, "role": "assistant"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": mid, "delta": p.text},
            {"type": "TEXT_MESSAGE_END", "messageId": mid},
        ]
    return [
        {"type": "REASONING_START", "messageId": mid},
        {"type": "REASONING_MESSAGE_START", "messageId": mid, "role": "reasoning"},
        {"type": "REASONING_MESSAGE_CONTENT", "messageId": mid, "delta": p.text},
        {"type": "REASONING_MESSAGE_END", "messageId": mid},
        {"type": "REASONING_END", "messageId": mid},
    ]


def _result(e: ToolResultEvent | ToolResultLateEvent) -> Chunk:
    return {
        "type": "TOOL_CALL_RESULT",
        "messageId": e.event_id,
        "toolCallId": e.data.call_id,
        "content": e.data.preview,
        "role": "tool",
    }


def started(s: Spawned) -> Chunk:
    """A running legacy subagent: its start event, also sent in a replay's preamble."""
    return {
        "type": "SUBAGENT_STARTED",
        "subagentRunId": s.child,
        "name": s.agent,
        "parentToolCallId": s.call_id,
    }
