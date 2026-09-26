import { z } from "zod";
import { agent, scriptedModel, sqlite, tool } from "../../src";

// The other process of the tree-cancel drill (cancel-tree-cross-process.test.ts): it holds both
// the parent's and its child's leases. It prints `running` once the child is inside a tool that
// waits, releases that tool on any stdin line, and prints `done <status>` when its run ends.

const path = process.argv[2];
if (path === undefined) throw new Error("usage: cancel-tree-worker <store>");

const usage = { input_tokens: 10, output_tokens: 2 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
const use = (name: string, input: Record<string, unknown>, id: string) => ({
  content: [{ type: "tool_use", call_id: id, name, input }],
  stop_reason: "tool_use",
  usage,
});

const { promise: gate, resolve: open } = Promise.withResolvers<void>();
const slow = tool({
  name: "slow",
  description: "Waits.",
  input: z.object({}),
  runs: "host",
  effect: "read_only",
  execute: async () => {
    process.stdout.write("running\n");
    await gate;
    return "ok";
  },
});
const worker = agent({
  name: "worker",
  model: scriptedModel({ responses: [use("slow", {}, "w1"), say("x")] }),
  tools: [slow],
});
const lead = agent({
  name: "lead",
  model: scriptedModel({
    responses: [
      use("spawn_agent", { agent: "worker", prompt: "Do it." }, "s1"),
      say("never"),
    ],
  }),
  // The lead lists `slow` too: a child only gets the tools its parent has.
  tools: [slow],
  subagents: [worker],
});

const running = lead.run("Go.", { store: sqlite(path) });
for await (const line of console) if (line.trim() !== "") break;
open();
const result = await running;
process.stdout.write(`done ${result.status}\n`);
