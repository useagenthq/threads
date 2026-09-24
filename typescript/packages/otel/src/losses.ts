import type { LossRow } from "@threads/core/internal/feed";
import { lossIds } from "./ids";
import { INTERNAL, type Span } from "./span";

/** One zero-duration threads.export.possibly_lost span per loss row, at its deleted_at. */
export function lossSpans(
  observer: string,
  rows: readonly LossRow[],
): readonly Span[] {
  return rows.map((row) => ({
    ...lossIds(observer, row.thread_id, row.deleted_at),
    branchId: "",
    closeSeq: 0,
    parentSpanId: undefined,
    name: "threads.export.possibly_lost",
    kind: INTERNAL,
    start: row.deleted_at,
    end: row.deleted_at,
    attributes: {
      "threads.tenant": row.tenant_id,
      "threads.thread_id": row.thread_id,
      "threads.unchecked_events": row.unchecked_events,
      "threads.deleted_at": row.deleted_at,
    },
    events: [],
    links: [],
    status: undefined,
  }));
}
