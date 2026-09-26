import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { agentCases } from "./agent-cases";
import type { Decision, HookCase } from "./kit";
import { toolCases } from "./tool-cases";
import { turnCases } from "./turn-cases";

// One direct test per hook (lane 14 E), each driving a public agent().run() in the situation
// that fires it. The registry is keyed by the hook names spec/api.json lists, so the guard below
// fails by construction when a hook is added and left untested, and every case's normalized
// hook_decisions are compared with Python's through spec/conformance/vectors/hook-decisions.json.

/** Every hook's case, keyed as spec/api.json `types.Hooks` names it. */
const cases: Record<string, HookCase> = {
  ...turnCases,
  ...toolCases,
  ...agentCases,
};

const spec = (name: string): unknown =>
  JSON.parse(
    readFileSync(join(import.meta.dir, "../../../../../spec", name), "utf8"),
  );

const HOOKS: readonly string[] = Object.keys(
  z
    .object({
      types: z.object({
        Hooks: z.object({ fields: z.record(z.string(), z.unknown()) }),
      }),
    })
    .parse(spec("api.json")).types.Hooks.fields,
);

/** The wire `hook_decision.hook`; only on_stop_failure is named differently there. */
const wireName = (hook: string): string =>
  hook === "on_stop_failure" ? "stop_failure" : hook;

const Row = z.record(z.string(), z.union([z.string(), z.number()]));
const Vector = z.object({
  doc: z.string(),
  known_divergences: z.array(
    z.object({ hook: z.string(), field: z.string(), note: z.string() }),
  ),
  decisions: z.record(z.string(), z.array(Row)),
});
const vector = Vector.parse(spec("conformance/vectors/hook-decisions.json"));

/** The fields a hook's decision is not yet compared on, as the vector records. */
const divergent = (hook: string): ReadonlySet<string> =>
  new Set(
    vector.known_divergences.flatMap((d) =>
      d.hook === wireName(hook) ? [d.field] : [],
    ),
  );

const shed = (
  hook: string,
  rows: readonly (Decision | z.infer<typeof Row>)[],
): readonly Record<string, unknown>[] => {
  const drop = divergent(hook);
  return rows.map((row) =>
    Object.fromEntries(
      Object.entries(row).filter(([field]) => !drop.has(field)),
    ),
  );
};

test("every hook spec/api.json lists has a case", () => {
  expect(Object.keys(cases).toSorted()).toEqual([...HOOKS].toSorted());
});

test("the vector holds the decisions of every hook's case", () => {
  expect(Object.keys(vector.decisions).toSorted()).toEqual(
    [...HOOKS].toSorted(),
  );
});

describe("each hook, on a public agent().run()", () => {
  for (const [hook, run] of Object.entries(cases))
    test(hook, async () => {
      const produced = await run();
      // Whatever the case decided is recorded under the wire hook name.
      expect([...new Set(produced.map((d) => d.hook))]).toEqual(
        produced.length === 0 ? [] : [wireName(hook)],
      );
      expect(shed(hook, produced)).toEqual([
        ...shed(hook, vector.decisions[hook] ?? []),
      ]);
    });
});
