import { canonicalize, type Json } from "@threads/core/internal/feed";
import { SEMCONV } from "./attrs";
import type { Attrs, AttrValue, Span, SpanEvent } from "./span";

// OTLP/HTTP JSON (spec/otel/README.md, "OTLP/HTTP JSON encoding"): canonical JSON, lowercase hex
// ids, decimal-string times and integers, attributes sorted by key, flags only when true.

export const SCHEMA_URL: string = `https://opentelemetry.io/schemas/${SEMCONV}`;
export const MAX_BATCH = 512;

function value(v: AttrValue): Json {
  if (typeof v === "boolean") return { boolValue: v };
  if (typeof v === "number") return { intValue: String(v) };
  if (typeof v === "string") return { stringValue: v };
  return { arrayValue: { values: v.map((s) => ({ stringValue: s })) } };
}

export function attributes(attrs: Attrs): Json {
  return Object.entries(attrs)
    .filter(
      (entry): entry is [string, AttrValue] =>
        entry[1] !== undefined && entry[1] !== false,
    )
    .toSorted(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([key, v]) => ({ key, value: value(v) }));
}

const nanos = (ms: number): string => `${ms}000000`;

function event(e: SpanEvent): Json {
  return {
    timeUnixNano: nanos(e.time),
    name: e.name,
    attributes: attributes(e.attributes),
  };
}

function span(s: Span): Json {
  return {
    traceId: s.traceId,
    spanId: s.spanId,
    parentSpanId: s.parentSpanId,
    name: s.name,
    kind: s.kind,
    startTimeUnixNano: nanos(s.start),
    endTimeUnixNano: nanos(s.end),
    attributes: attributes(s.attributes),
    events: s.events.length > 0 ? s.events.map(event) : undefined,
    links:
      s.links.length > 0
        ? s.links.map((l) => ({ traceId: l.traceId, spanId: l.spanId }))
        : undefined,
    status: s.status === undefined ? undefined : { code: 2, message: s.status },
  };
}

/** The (branch, close_seq, span id) order every body uses. */
export function ordered(spans: readonly Span[]): readonly Span[] {
  const key = (s: Span): readonly [string, number, string] => [
    s.branchId,
    s.closeSeq,
    s.spanId,
  ];
  return spans.toSorted((x, y) => {
    const [a, b] = [key(x), key(y)];
    if (a[0] !== b[0]) return a[0] < b[0] ? -1 : 1;
    if (a[1] !== b[1]) return a[1] - b[1];
    return a[2] < b[2] ? -1 : a[2] > b[2] ? 1 : 0;
  });
}

/** One request body: canonical JSON of the spans under one resource and scope. */
export function body(
  spans: readonly Span[],
  resource: Readonly<Record<string, string>>,
  version: string,
): string {
  const doc: Json = {
    resourceSpans: [
      {
        resource: { attributes: attributes(resource) },
        scopeSpans: [
          {
            schemaUrl: SCHEMA_URL,
            scope: { name: "threads", version },
            spans: ordered(spans).map(span),
          },
        ],
      },
    ],
  };
  const text = canonicalize(doc);
  if (!text.ok)
    throw new Error(
      `an OTLP body is not canonical JSON: ${text.error.message}`,
    );
  return text.value;
}

/**
 * Batches of at most MAX_BATCH spans in order, never splitting the spans of one branch that
 * share a close_seq (a park closes a turn and its tool span at once), even past the limit.
 */
export function batches(spans: readonly Span[]): readonly (readonly Span[])[] {
  const out: Span[][] = [];
  let batch: Span[] = [];
  const all = ordered(spans);
  all.forEach((s, i) => {
    const prev = all[i - 1];
    const joined =
      prev !== undefined &&
      prev.branchId === s.branchId &&
      prev.closeSeq === s.closeSeq;
    if (!joined && batch.length >= MAX_BATCH) {
      out.push(batch);
      batch = [];
    }
    batch.push(s);
  });
  if (batch.length > 0) out.push(batch);
  return out;
}
