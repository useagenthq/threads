"""OTLP/HTTP JSON (spec/otel/README.md, "OTLP/HTTP JSON encoding"): canonical JSON, lowercase
hex ids, decimal-string times and integers, attributes sorted by key, flags only when true."""

from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import JsonValue

from threads.log.jcs import canonicalize
from threads.otel.attrs import SEMCONV
from threads.otel.span import Attrs, AttrValue, Span, SpanEvent
from threads.result import Err

SCHEMA_URL: Final = f"https://opentelemetry.io/schemas/{SEMCONV}"
MAX_BATCH: Final = 512


def _value(v: AttrValue) -> JsonValue:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, str):
        return {"stringValue": v}
    return {"arrayValue": {"values": [{"stringValue": s} for s in v]}}


def attributes(attrs: Mapping[str, AttrValue | None]) -> list[JsonValue]:
    return [
        {"key": k, "value": _value(v)}
        for k, v in sorted(attrs.items())
        if v is not None and v is not False
    ]


def _nanos(ms: int) -> str:
    return f"{ms}000000"


def _event(e: SpanEvent) -> JsonValue:
    return {"timeUnixNano": _nanos(e.time), "name": e.name, "attributes": attributes(e.attributes)}


def _span(s: Span) -> JsonValue:
    out: dict[str, JsonValue] = {
        "traceId": s.trace_id,
        "spanId": s.span_id,
        "name": s.name,
        "kind": s.kind,
        "startTimeUnixNano": _nanos(s.start),
        "endTimeUnixNano": _nanos(s.end),
        "attributes": attributes(s.attributes),
    }
    if s.parent_span_id is not None:
        out["parentSpanId"] = s.parent_span_id
    if s.events:
        out["events"] = [_event(e) for e in s.events]
    if s.links:
        out["links"] = [{"traceId": link.trace_id, "spanId": link.span_id} for link in s.links]
    if s.status is not None:
        out["status"] = {"code": 2, "message": s.status}
    return out


def ordered(spans: Sequence[Span]) -> list[Span]:
    """The (branch, close_seq, span id) order every body uses."""
    return sorted(spans, key=lambda s: (s.branch_id, s.close_seq, s.span_id))


def body(spans: Sequence[Span], resource: Mapping[str, str], version: str) -> bytes:
    """One request body: canonical JSON of the spans under one resource and scope."""
    resource_attrs: Attrs = dict(resource)
    doc: JsonValue = {
        "resourceSpans": [
            {
                "resource": {"attributes": attributes(resource_attrs)},
                "scopeSpans": [
                    {
                        "schemaUrl": SCHEMA_URL,
                        "scope": {"name": "threads", "version": version},
                        "spans": [_span(s) for s in ordered(spans)],
                    }
                ],
            }
        ]
    }
    text = canonicalize(doc)
    if isinstance(text, Err):
        raise ValueError(f"an OTLP body is not canonical JSON: {text.error}")
    return text.value.encode()


def batches(spans: Sequence[Span]) -> list[list[Span]]:
    """Batches of at most MAX_BATCH spans in order, never splitting the spans of one branch that
    share a close_seq (a park closes a turn and its tool span at once), even past the limit."""
    out: list[list[Span]] = []
    batch: list[Span] = []
    previous: Span | None = None
    for s in ordered(spans):
        joined = (
            previous is not None
            and previous.branch_id == s.branch_id
            and previous.close_seq == s.close_seq
        )
        if not joined and len(batch) >= MAX_BATCH:
            out.append(batch)
            batch = []
        batch.append(s)
        previous = s
    if batch:
        out.append(batch)
    return out
