import { describe, expect, test } from "bun:test";
import { encode, expectedBody, goldens, synced } from "./goldens";

// Every golden's bytes, sync by sync: TypeScript sends exactly what spec/otel pins, and Python
// the same bytes (python/tests/otel/test_goldens.py).

describe("otel goldens", () => {
  for (const g of goldens())
    test(g.name, () => {
      for (const s of g.syncs) {
        const sent = synced(g, s.branch_id, s.cursor_before, s.head_seq);
        expect(sent).toHaveLength(s.spans);
        expect(encode(g, sent)).toBe(expectedBody(g, s.expected));
      }
    });
});

// A span's bytes depend only on events up to its close: a sync after every append sends the same
// spans, byte for byte, as one sync at the end.
describe("otel goldens at every tick position", () => {
  for (const g of goldens())
    test(g.name, () => {
      for (const b of new Set(g.syncs.map((s) => s.branch_id))) {
        const mine = g.syncs.filter((s) => s.branch_id === b);
        const from = mine[0]?.cursor_before ?? 0;
        const head = mine.at(-1)?.head_seq ?? 0;
        const once = synced(g, b, from, head).map((s) => encode(g, [s]));
        const ticked = Array.from({ length: head - from }, (_, i) =>
          synced(g, b, from + i, from + i + 1),
        ).flatMap((sent) => sent.map((s) => encode(g, [s])));
        expect(ticked.toSorted()).toEqual(once.toSorted());
      }
    });
});
