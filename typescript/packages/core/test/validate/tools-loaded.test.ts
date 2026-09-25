import { describe, expect, test } from "bun:test";
import { KnownEvent } from "../../src/log";
import { loadCase } from "../conformance/cases";
import { fixture, unwrap } from "../store/helpers";
import { draftOf } from "../team/writes";

// Rule 47 and rule 17 point 6 on the write path: each rejection case's log, appended again
// event by event through a writer, is refused at exactly its last event.

const CASES = [
  "tools-loaded-without-search-rejected",
  "tools-loaded-not-adjacent-rejected",
  "tools-loaded-after-error-rejected",
  "tools-loaded-undeferred-rejected",
  "tools-loaded-twice-rejected",
  "tools-loaded-ref-mismatch-rejected",
  "tools-loaded-per-call-twice-rejected",
  "tools-changed-full-form-before-load-rejected",
  "tools-changed-ref-form-after-load-rejected",
];

describe("a writer refuses what import refuses", () => {
  for (const name of CASES)
    test(name, async () => {
      const c = loadCase(name);
      const lines = new TextDecoder()
        .decode(c.log)
        .trimEnd()
        .split("\n")
        .slice(1, -1)
        .map((l) => KnownEvent.parse(JSON.parse(l)));
      const [first, ...rest] = lines;
      const last = rest.pop();
      if (first === undefined || last === undefined) throw new Error("a log");
      const fx = await fixture();
      const opened = await fx.store.openBranch({
        threadId: first.thread_id,
        branchId: first.branch_id,
        lease: { holderId: "t", ttlMs: 1e12 },
        drafts: [draftOf(first)],
      });
      const writer = unwrap(opened);
      if (typeof writer === "string") throw new Error("already open");
      for (const e of rest) unwrap(await writer.append([draftOf(e)]));
      const refused = await writer.append([draftOf(last)]);
      expect(refused.ok ? "ok" : refused.error.code).toBe("invalid_transition");
    });
});
