import {
  type Agent,
  agent,
  type Model,
  scriptedModel,
  tool,
} from "@threads/core";
import { z } from "zod";

// The `--agent` module of the simulated-user CLI tests (spec lane 32, E): agents, a judge, a
// budget and the `user` model that plays the customer. Everything is scripted.

const usage = { input_tokens: 10, output_tokens: 2 };
const use = (name: string, input: Record<string, unknown>, id: string) => ({
  content: [{ type: "tool_use", call_id: id, name, input }],
  stop_reason: "tool_use",
  usage,
});
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});

const lookup = tool({
  name: "lookup_order",
  description: "Look up an order by id.",
  input: z.object({ id: z.string() }),
  effect: "read_only",
  execute: async ({ id }) => `order ${id}: shipped`,
});

/** One turn: look the order up, then answer. A call id is used once per thread. */
const turn = (n: number) => [
  use("lookup_order", { id: "42" }, `r${n}`),
  say("Order 42 shipped; it is inside the 30-day window."),
];

export const TURNS: readonly unknown[] = [turn(0), turn(1), turn(2)].flat();

export function support(): Agent {
  return agent({
    name: "support",
    instructions: "You answer order questions.",
    model: scriptedModel({ responses: TURNS }),
    tools: [lookup],
    permissions: { allow: ["lookup_order"] },
  });
}

const agents: readonly Agent[] = [support()];
export default agents;

export const judge: Model = scriptedModel({
  responses: [
    use(
      "final_output",
      {
        verdicts: [
          { criterion: 1, pass: true, reason: "It quotes the window." },
        ],
      },
      "v1",
    ),
  ],
});

/** Plays the customer: one follow-up, then done. */
export const user: Model = scriptedModel({
  responses: [
    use("final_output", { message: "What about a repair?", done: false }, "u1"),
    use("final_output", { message: "", done: true }, "u2"),
  ],
});

export const budget: { max_model_requests: number } = {
  max_model_requests: 20,
};
