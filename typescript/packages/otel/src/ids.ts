import { sha256Hex } from "@threads/core/internal/feed";

// Span and trace ids derived from the log (spec/otel/README.md, "Ids"): a re-send carries the
// same ids, and both implementations derive the same ones.

/** An all-zero id is invalid in OpenTelemetry: its last byte becomes 1. */
export function nonzero(hex: string): string {
  return /^0+$/.test(hex) ? `${hex.slice(0, -2)}01` : hex;
}

function digest(tag: string, parts: readonly string[]): string {
  return sha256Hex([`threads-${tag}`, ...parts].join("\0"));
}

/** The span a log event opens; a continuation also names its call. */
export function spanId(
  branchId: string,
  eventId: string,
  callId?: string,
): string {
  const parts =
    callId === undefined ? [branchId, eventId] : [branchId, eventId, callId];
  return nonzero(digest("span", parts).slice(0, 16));
}

/** The trace rooted at an event of a thread. */
export function traceId(threadId: string, eventId: string): string {
  return nonzero(digest("trace", [threadId, eventId]).slice(0, 32));
}

/** The ids of a threads.export.possibly_lost span. */
export function lossIds(
  observer: string,
  threadId: string,
  deletedAt: number,
): { readonly traceId: string; readonly spanId: string } {
  return {
    traceId: nonzero(digest("loss", [observer, threadId]).slice(0, 32)),
    spanId: nonzero(
      digest("loss", [observer, threadId, String(deletedAt)]).slice(0, 16),
    ),
  };
}
