// Simulated users: save a real conversation, then grade the current agent against a user who
// keeps talking. Needs: nothing (scripted models stand in for real ones; no API keys, no network).
// Run: cd typescript && bun examples/evals-simulate.ts
//
// For the live smoke, swap each scriptedModel for anthropic("claude-haiku-4-5") and pass a
// conversation budget: {max_input_tokens: 150_000, max_output_tokens: 15_000}. Never in CI.

import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { agent, runEvals, scriptedModel, sqlite, tool } from "@threads/core";
import { z } from "zod";

const usage = { input_tokens: 10, output_tokens: 2 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
const call = (name: string, input: Record<string, unknown>, id: string) => ({
  content: [{ type: "tool_use", call_id: id, name, input }],
  stop_reason: "tool_use",
  usage,
});

const lookupOrder = tool({
  name: "lookup_order",
  description: "Look up an order by id.",
  input: z.object({ id: z.string() }),
  effect: "read_only",
  execute: async ({ id }) => `order ${id}: delivered 40 days ago`,
});

/**
 * One turn: look the order up, then answer. A call id is used once per thread, and a continued
 * prefix puts the recorded ids on the live thread too, so the live agent needs its own.
 */
const turn = (prefix: string, n: number, answer: string) => [
  call("lookup_order", { id: "42" }, `${prefix}${n}`),
  say(answer),
];

const REFUSED =
  "Order 42 arrived 40 days ago, outside the 30-day refund window.";
const REPAIR = "A free repair is available instead; shall I book it?";
const BOOKED = "Booked the repair for order 42.";

function support(ids: string, answers: readonly string[]) {
  return agent({
    name: "support",
    instructions:
      "You answer refund questions. Quote the 30-day refund window.",
    model: scriptedModel({
      responses: answers.flatMap((answer, i) => turn(ids, i, answer)),
    }),
    tools: [lookupOrder],
    permissions: { allow: ["lookup_order"] },
  });
}

/** The customer: pushes back twice, then stops. Live, this is one model call per message. */
const customer = scriptedModel({
  responses: [
    {
      ...call(
        "final_output",
        { message: "That seems harsh.", done: false },
        "u1",
      ),
    },
    {
      ...call(
        "final_output",
        { message: "Any other option?", done: false },
        "u2",
      ),
    },
    { ...call("final_output", { message: "", done: true }, "u3") },
  ],
});

const judge = scriptedModel({
  responses: [
    call(
      "final_output",
      {
        verdicts: [
          { criterion: 1, pass: true, reason: "It quotes the window." },
          { criterion: 2, pass: true, reason: "It offers the repair." },
        ],
      },
      "v1",
    ),
  ],
});

export async function main(): Promise<string> {
  const cases = mkdtempSync(join(tmpdir(), "cases-"));

  // 1. A real conversation, saved once at the turn the customer pushed back. Everything before
  //    that turn is the prefix; the turn's own text is the simulation's first user message.
  const recorded = support("r", [REFUSED, REPAIR]);
  const store = sqlite(":memory:");
  const first = await recorded.run("Can I still return order 42?", { store });
  const pushback = await recorded.run("But it only broke yesterday.", {
    store,
    thread: first.thread,
  });
  const saved = await pushback.thread.saveCase("refund-pushback", {
    expect: { must: [{ type: "tool_call", data: { name: "lookup_order" } }] },
    externalEffects: "stub",
    simulate: {
      kind: "model",
      persona:
        "A customer who bought headphones 40 days ago. Polite but persistent.",
      goal: "Get a refund, or a clear reason why not and what else is possible.",
      maxMessages: 3,
    },
    rubric: [
      "Never promises a refund outside the 30-day window",
      "Offers the repair option once the refund is refused",
    ],
    dir: cases,
  });
  if (!saved.ok) throw new Error(saved.error.message);

  // 2. Live: the current agent talks to the simulated customer, and the judge grades it all.
  const graded = await runEvals({
    cases,
    agents: [support("s", [REFUSED, REPAIR, BOOKED])],
    live: { judge, user: customer, budget: { max_model_requests: 40 } },
  });
  return graded.summary;
}

if (import.meta.main) console.log(await main());
// Output: 1 passed, 0 failed; 10 model calls (6 agent, 3 user, 1 judge), cost unknown; refund-pushback: 3 messages, user done
