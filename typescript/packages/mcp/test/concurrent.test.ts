import { describe, expect, test } from "bun:test";
import { agent, scriptedModel, sqlite, tool } from "@threads/core";
import { z } from "zod";
import { bound, say, server, session } from "./kit";

// Parallel tool calls: an MCP tool is never concurrent, so it runs alone between two
// concurrent app reads (spec/schema/README.md, Parallel tool calls).

describe("MCP tools and tool.concurrent", () => {
  test("an MCP binding is never concurrent", async () => {
    const h = server();
    for (const t of (await session(h)).tools)
      expect((await bound(h, t.name)).concurrent).toBeUndefined();
  });

  test("an MCP call between two concurrent reads runs alone", async () => {
    const trace: string[] = [];
    const traced = (name: string) =>
      tool({
        name,
        description: `The ${name} tool.`,
        input: z.object({}),
        effect: "read_only",
        concurrent: true,
        execute: async () => {
          trace.push(`start ${name}`);
          await Promise.resolve();
          trace.push(`end ${name}`);
          return name;
        },
      });
    const response = {
      content: [
        { type: "tool_use", call_id: "c1", name: "a", input: {} },
        {
          type: "tool_use",
          call_id: "c2",
          name: "mcp__docs__search",
          input: { query: "refunds" },
        },
        { type: "tool_use", call_id: "c3", name: "b", input: {} },
      ],
      stop_reason: "tool_use",
      usage: { input_tokens: 10, output_tokens: 2 },
    };
    const bot = agent({
      model: scriptedModel({ responses: [response, say("done")] }),
      tools: [traced("a"), server(), traced("b")],
      permissions: { mode: "bypass" },
    });
    const result = await bot.run("go", { store: sqlite(":memory:") });
    expect(result.status).toBe("completed");
    expect(trace.indexOf("end a")).toBeLessThan(trace.indexOf("start b"));
  });
});
