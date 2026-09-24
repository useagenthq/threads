# pyright: strict
"""The expected OTLP spans of the OpenTelemetry goldens (spec/otel/README.md).

Each case names its spans by hand: which events open and close them, their trace, parent,
links and span events. This module only turns that choice into the pinned bytes (ids,
attributes, encoding), so a golden states the expectation instead of re-deriving it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .common import num, obj, text
from .jcs import JsonValue, Obj, canonical

SEMCONV = "1.41.1"
SCHEMA_URL = f"https://opentelemetry.io/schemas/{SEMCONV}"
SCOPE_VERSION = "0.0.0"
INTERNAL, CLIENT = 1, 3
OK_REASONS = frozenset({"end_turn", "cancelled", "handoff", "input_denied"})
PROVIDERS = {
    "bedrock": "aws.bedrock",
    "vertex": "gcp.vertex_ai",
    "azure": "azure.ai.openai",
}
RESOURCE: Obj = {
    "service.name": "golden",
    "telemetry.sdk.language": "golden",
    "telemetry.sdk.name": "threads",
    "threads.otel.semconv": SEMCONV,
}


def _digest(tag: str, *parts: str) -> bytes:
    return hashlib.sha256("\0".join((f"threads-{tag}", *parts)).encode()).digest()


def nonzero(b: bytes) -> str:
    """An all-zero id is invalid in OpenTelemetry: its last byte becomes 1."""
    return (b[:-1] + b"\x01" if not any(b) else b).hex()


def span_id(branch: str, event_id: str, call_id: str | None = None) -> str:
    parts = (branch, event_id) if call_id is None else (branch, event_id, call_id)
    return nonzero(_digest("span", *parts)[:8])


def trace_id(thread: str, event_id: str) -> str:
    return nonzero(_digest("trace", thread, event_id)[:16])


def loss_ids(observer: str, thread: str, deleted_at: int) -> tuple[str, str]:
    trace = nonzero(_digest("loss", observer, thread)[:16])
    return trace, nonzero(_digest("loss", observer, thread, str(deleted_at))[:8])


def root_of(e: Obj) -> str:
    """The trace a turn opener is the root of."""
    return trace_id(text(e["thread_id"]), text(e["event_id"]))


type Attr = str | int | bool | list[str] | None


def _value(v: str | int | bool | list[str]) -> Obj:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, str):
        return {"stringValue": v}
    return {"arrayValue": {"values": [{"stringValue": s} for s in v]}}


def attributes(d: dict[str, Attr]) -> list[JsonValue]:
    """Sorted by key; None and False are left out (a flag is present only when true)."""
    return [
        {"key": k, "value": _value(v)}
        for k, v in sorted(d.items())
        if v is not None and v is not False
    ]


def nanos(e: Obj) -> str:
    return str(num(e["time"]) * 1_000_000)


# Span event attributes by event type: (attribute, data field).
EVENT_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "permission_decision": (
        ("threads.permission.decision", "decision"),
        ("threads.permission.source", "source"),
    ),
    "approval_requested": (("threads.call_id", "call_id"),),
    "approval_granted": (("threads.call_id", "call_id"),),
    "approval_denied": (("threads.call_id", "call_id"),),
    "effect_begin": (("threads.call_id", "call_id"), ("threads.effect.attempt", "attempt")),
    "effect_commit": (("threads.call_id", "call_id"),),
    "effect_unknown": (("threads.call_id", "call_id"), ("threads.effect.reason", "reason")),
    "effect_resolved": (
        ("threads.call_id", "call_id"),
        ("threads.effect.outcome", "outcome"),
        ("threads.effect.by", "by"),
    ),
    "tool_result_late": (("threads.call_id", "call_id"),),
    "retry_scheduled": (("threads.retry.delay_ms", "delay_ms"),),
    "compacted": (("threads.compaction.trigger", "trigger"),),
    "compaction_failed": (
        ("threads.compaction.stage", "stage"),
        ("threads.compaction.reason", "reason"),
    ),
    "budget_exceeded": (("threads.budget.scope", "scope"), ("threads.budget.limit", "limit")),
}


def event(e: Obj, retry_of: str | None = None) -> Obj:
    """A span event: the log event's type and time, `threads.seq` and its table attributes
    (`threads.retry.of` is the failed attempt's span id, for a retry_scheduled)."""
    d = obj(e["data"])
    attrs: dict[str, Attr] = {"threads.seq": num(e["seq"]), "threads.retry.of": retry_of}
    for key, name in EVENT_FIELDS[text(e["type"])]:
        v = d.get(name)
        attrs[key] = v if isinstance(v, (str, int)) else None
    return {"timeUnixNano": nanos(e), "name": text(e["type"]), "attributes": attributes(attrs)}


@dataclass(frozen=True)
class Ctx:
    """What every span of one exported branch shares."""

    tenant: str
    branch: str
    agent: str = "demo"
    model: Obj = field(default_factory=lambda: {"provider": "scripted", "name": "scripted-1"})
    content: bool = False


@dataclass(frozen=True)
class Span:
    close_seq: int
    json: Obj

    @property
    def id(self) -> str:
        return text(self.json["spanId"])

    @property
    def trace(self) -> str:
        return text(self.json["traceId"])


@dataclass(frozen=True)
class Shape:
    """A span's identity and place in its trace."""

    sid: str
    trace: str
    parent: str | None = None
    events: tuple[Obj, ...] = ()
    links: tuple[Span, ...] = ()


def span(  # noqa: PLR0913, PLR0917 - one span's parts
    ctx: Ctx,
    shape: Shape,
    name: str,
    kind: int,
    ends: tuple[Obj, Obj],
    attrs: dict[str, Attr],
    status: str | None = None,
) -> Span:
    start, end = ends
    skew = num(end["time"]) < num(start["time"])
    common: dict[str, Attr] = {
        "threads.tenant": ctx.tenant,
        "threads.thread_id": text(start["thread_id"]),
        "threads.branch_id": ctx.branch,
        "threads.seq.start": num(start["seq"]),
        "threads.seq.end": num(end["seq"]),
        "threads.clock_skew": skew,
    }
    j: Obj = {
        "traceId": shape.trace,
        "spanId": shape.sid,
        "name": name,
        "kind": kind,
        "startTimeUnixNano": nanos(start),
        "endTimeUnixNano": nanos(start if skew else end),
        "attributes": attributes({**common, **attrs}),
    }
    if shape.parent is not None:
        j["parentSpanId"] = shape.parent
    if shape.events:
        j["events"] = list(shape.events)
    if shape.links:
        j["links"] = [{"traceId": s.trace, "spanId": s.id} for s in shape.links]
    if status is not None:
        j["status"] = {"code": 2, "message": status}
    return Span(num(end["seq"]), j)


def body(spans: list[Span]) -> bytes:
    """One OTLP/HTTP JSON request body, spans in (branch, close_seq, span id) order."""
    ordered = sorted(
        spans,
        key=lambda s: (_branch(s), s.close_seq, s.id),
    )
    resource: Obj = {"attributes": attributes({k: text(v) for k, v in RESOURCE.items()})}
    scope: Obj = {
        "schemaUrl": SCHEMA_URL,
        "scope": {"name": "threads", "version": SCOPE_VERSION},
        "spans": [s.json for s in ordered],
    }
    return canonical({"resourceSpans": [{"resource": resource, "scopeSpans": [scope]}]})


def _branch(s: Span) -> str:
    for a in s.json["attributes"] if isinstance(s.json["attributes"], list) else []:
        if obj(a)["key"] == "threads.branch_id":
            return text(obj(obj(a)["value"])["stringValue"])
    raise ValueError("a span without threads.branch_id")
