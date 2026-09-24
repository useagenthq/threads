"""Attributes by semantic conventions core v1.41.1 (spec/otel/README.md, "Attributes")."""

from typing import Final

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    ApprovalDeniedEvent,
    ApprovalGrantedEvent,
    ApprovalRequestedEvent,
    BudgetExceededEvent,
    CompactedEvent,
    CompactionFailedEvent,
    EffectBeginEvent,
    EffectCommitEvent,
    EffectResolvedEvent,
    EffectUnknownEvent,
    Event,
    ModelRef,
    ModelRequestEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    PermissionDecisionEvent,
    RetryScheduledEvent,
    TextPart,
    ToolCallEvent,
    ToolResultLateEvent,
    Usage,
)
from threads.log.jcs import canonicalize
from threads.otel.span import Attrs, SpanEvent
from threads.result import Ok

SEMCONV: Final = "1.41.1"

_PROVIDERS: Final = {
    "bedrock": "aws.bedrock",
    "vertex": "gcp.vertex_ai",
    "azure": "azure.ai.openai",
}
_OK_REASONS: Final = frozenset({"end_turn", "cancelled", "handoff", "input_denied"})
"""Turn endings that are not failures; every other reason is status ERROR."""


def turn_attrs(agent: str, thread_id: str, run_id: str | None, parent_missing: bool) -> Attrs:
    return {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": agent,
        "gen_ai.conversation.id": thread_id,
        "threads.run_id": run_id,
        "threads.parent_missing": parent_missing or None,
    }


def turn_status(reason: str) -> str | None:
    """A turn_completed's status message: None unless the reason is a failure."""
    return None if reason in _OK_REASONS else reason


def chat_attrs(model: ModelRef, request: ModelRequestEvent) -> Attrs:
    purpose = request.data.purpose
    return {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": _PROVIDERS.get(model.provider, model.provider),
        "gen_ai.request.model": model.name,
        "threads.model.attempt": request.data.attempt,
        "threads.model.purpose": "turn" if purpose is MISSING else purpose,
    }


def _field(value: object) -> int | None:
    """A usage field as sent: absent (MISSING) and null (None) both leave it out."""
    return value if isinstance(value, int) else None


def usage_attrs(u: Usage) -> Attrs:
    """Cached input counts toward input_tokens. An absent cache field (no such billing
    category) is 0; a null part (it never arrived) leaves the total out."""
    parts = (
        u.input_tokens,
        0 if u.cache_read_tokens is MISSING else u.cache_read_tokens,
        0 if u.cache_write_tokens is MISSING else u.cache_write_tokens,
    )
    total = sum(p for p in parts if p is not None) if all(p is not None for p in parts) else None
    return {
        "gen_ai.usage.input_tokens": total,
        "gen_ai.usage.cache_read.input_tokens": _field(u.cache_read_tokens),
        "gen_ai.usage.cache_creation.input_tokens": _field(u.cache_write_tokens),
        "gen_ai.usage.output_tokens": _field(u.output_tokens),
        "gen_ai.usage.reasoning.output_tokens": _field(u.reasoning_tokens),
    }


def response_attrs(
    response: ModelResponseEvent | ModelResponseRecoveredEvent, content: bool
) -> Attrs:
    text = (
        "".join(p.text for p in response.data.content if isinstance(p, TextPart))
        if content
        else None
    )
    return {
        "gen_ai.response.finish_reasons": (response.data.stop_reason,),
        **usage_attrs(response.data.usage),
        "threads.model.output_text": text,
    }


def tool_attrs(
    call: ToolCallEvent, effect_class: str | None, resumed: bool, content: bool
) -> Attrs:
    args = canonicalize(dict(call.data.input)) if content else None
    return {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": call.data.name,
        "gen_ai.tool.call.id": call.data.call_id,
        "gen_ai.tool.type": "function",
        "threads.tool.effect_class": effect_class,
        "threads.resumed": resumed or None,
        "gen_ai.tool.call.arguments": args.value if isinstance(args, Ok) else None,
    }


def span_event(e: Event, retry_of: str | None = None) -> SpanEvent:
    """A span event: the log event's type and time, `threads.seq` and its table attributes."""
    attrs: Attrs = {"threads.seq": e.seq, "threads.retry.of": retry_of, **_event_attrs(e)}
    return SpanEvent(e.time, e.type, attrs)


def _event_attrs(e: Event) -> Attrs:
    if isinstance(e, PermissionDecisionEvent):
        return {
            "threads.permission.decision": e.data.decision,
            "threads.permission.source": e.data.source,
        }
    if isinstance(e, EffectBeginEvent):
        return {"threads.call_id": e.data.call_id, "threads.effect.attempt": e.data.attempt}
    if isinstance(e, EffectUnknownEvent):
        return {"threads.call_id": e.data.call_id, "threads.effect.reason": e.data.reason}
    if isinstance(e, EffectResolvedEvent):
        return {
            "threads.call_id": e.data.call_id,
            "threads.effect.outcome": e.data.outcome,
            "threads.effect.by": e.data.by,
        }
    return _other_attrs(e)


def _other_attrs(e: Event) -> Attrs:
    if isinstance(
        e,
        ApprovalRequestedEvent
        | ApprovalGrantedEvent
        | ApprovalDeniedEvent
        | EffectCommitEvent
        | ToolResultLateEvent,
    ):
        return {"threads.call_id": e.data.call_id}
    if isinstance(e, RetryScheduledEvent):
        return {"threads.retry.delay_ms": e.data.delay_ms}
    if isinstance(e, CompactedEvent):
        trigger = e.data.trigger
        return {"threads.compaction.trigger": None if trigger is MISSING else trigger}
    if isinstance(e, CompactionFailedEvent):
        return {
            "threads.compaction.stage": e.data.stage,
            "threads.compaction.reason": e.data.reason,
        }
    if isinstance(e, BudgetExceededEvent):
        return {"threads.budget.scope": e.data.scope, "threads.budget.limit": e.data.limit}
    return {}
