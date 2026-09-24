import {
  type Agent,
  agent,
  type McpServer,
  type Model,
  scriptedModel,
  tool,
} from "@threads/core";
import { z } from "zod";

// The `--agent` module of the `threads eval` tests: a support agent, a scripted judge and a
// budget. Everything is scripted: no test reaches a real model.

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

export const TURN: readonly unknown[] = [
  use("lookup_order", { id: "42" }, "c1"),
  say("Order 42 shipped; it is inside the 30-day window."),
];

const lookup = tool({
  name: "lookup_order",
  description: "Look up an order by id.",
  input: z.object({ id: z.string() }),
  effect: "read_only",
  execute: async ({ id }) => `order ${id}: shipped`,
});

/** Connection attempts on the MCP server below: a dry pin makes none. */
export const connects: { n: number } = { n: 0 };

const jira: McpServer = {
  kind: "mcp",
  name: "jira",
  connect: async () => {
    connects.n += 1;
    throw new Error("connection refused");
  },
};

export function support(withMcp = false): Agent {
  return agent({
    name: "support",
    instructions: "You answer order questions.",
    model: scriptedModel({ responses: TURN }),
    tools: withMcp ? [lookup, jira] : [lookup],
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

export const budget: { max_model_requests: number } = {
  max_model_requests: 10,
};
