// Easy evals: save a real turn as a case, then check it the way CI does, for free.
// Needs: nothing (a scripted model stands in for a real one; no API keys, no network).
// Run: cd typescript && bun examples/evals.ts

import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { agent, runEvals, scriptedModel, sqlite, tool } from "@threads/core";
import { z } from "zod";

const usage = { input_tokens: 10, output_tokens: 2 };

const lookupOrder = tool({
  name: "lookup_order",
  description: "Look up an order by id.",
  input: z.object({ id: z.string() }),
  effect: "read_only",
  execute: async ({ id }) => `order ${id}: delivered 12 days ago`,
});

const support = agent({
  name: "support",
  instructions: "You answer refund questions. Quote the 30-day refund window.",
  model: scriptedModel({
    responses: [
      {
        content: [
          {
            type: "tool_use",
            call_id: "c1",
            name: "lookup_order",
            input: { id: "42" },
          },
        ],
        stop_reason: "tool_use",
        usage,
      },
      {
        content: [
          {
            type: "text",
            text: "Order 42 arrived 12 days ago, inside the 30-day refund window.",
          },
        ],
        stop_reason: "end_turn",
        usage,
      },
    ],
  }),
  tools: [lookupOrder],
  permissions: { allow: ["lookup_order"] },
});

export async function main(): Promise<string> {
  const cases = mkdtempSync(join(tmpdir(), "cases-"));

  // 1. A real turn you liked, saved once as a case.
  const result = await support.run("Can I still return order 42?", {
    store: sqlite(":memory:"),
  });
  const saved = await result.thread.saveCase("refund-window", {
    expect: { must: [{ type: "tool_call", data: { name: "lookup_order" } }] },
    externalEffects: "stub",
    rubric: ["Quotes the 30-day refund window"],
    dir: cases,
  });
  if (!saved.ok) throw new Error(saved.error.message);

  // 2. In CI, for free: replay, rerun and drift against the agent as it is now.
  const report = await runEvals({ cases, agents: [support] });
  return report.summary;
}

if (import.meta.main) console.log(await main());
// Output: 1 passed, 0 failed
