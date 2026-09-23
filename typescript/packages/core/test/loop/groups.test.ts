import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { groups } from "../../src/loop/groups";

// The shared grouping vector (spec/conformance/vectors/tool-groups.json): both runtimes plan
// the same groups and barriers for the same pending calls.

const SPEC = join(import.meta.dir, "../../../../../spec");
const Call = z.strictObject({
  name: z.string(),
  effect_class: z.enum([
    "read_only",
    "idempotent",
    "reconcilable",
    "unguarded",
  ]),
  concurrent: z.boolean(),
  framework: z.boolean(),
  ends_turn: z.boolean(),
  decision: z.enum(["allow", "ask", "deny", "none"]),
});
const Vector = z.strictObject({
  description: z.string(),
  cases: z.array(
    z.strictObject({
      name: z.string(),
      pending: z.array(Call),
      plan: z.array(z.array(z.number().int())),
    }),
  ),
  recorded_order: z.array(z.string()),
});
const vector = Vector.parse(
  JSON.parse(
    readFileSync(join(SPEC, "conformance/vectors/tool-groups.json"), "utf8"),
  ),
);

describe("groups (tool-groups vector)", () => {
  for (const c of vector.cases)
    test(c.name, () => {
      const pending = c.pending.map((p) => ({
        concurrent: p.concurrent,
        effectClass: p.effect_class,
        framework: p.framework,
        endsTurn: p.ends_turn,
        decision: p.decision,
      }));
      expect(groups(pending)).toEqual(c.plan);
    });

  test("no pending calls, no plan", () => {
    expect(groups([])).toEqual([]);
  });
});
