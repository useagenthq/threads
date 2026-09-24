import { describe, expect, test } from "bun:test";
import { agent, scriptedModel, sqlite } from "@threads/core";
import { eventsOf, note, say, server, use } from "./kit";
import type { Call } from "./server";

// mcp({defer}) (spec/schema/README.md, "Deferred tools and tool_search"): every tool of the
// server is pinned in reference form, listed by tool_search, and callable once loaded.

describe("mcp({defer: true})", () => {
  test("the server's tools are deferred until tool_search loads them; then a call runs", async () => {
    const calls: Call[] = [];
    const bot = agent({
      model: scriptedModel({
        responses: [
          use("mcp__docs__search", { query: "refunds" }, "c0"),
          use("tool_search", { query: "mcp__docs__search" }, "c1"),
          use("mcp__docs__search", { query: "refunds" }, "c2"),
          say("done"),
        ],
      }),
      tools: [note, server({ calls, defer: true })],
      permissions: { mode: "bypass" },
    });
    const result = await bot.run("find refunds", { store: sqlite(":memory:") });
    expect(result.status).toBe("completed");
    const events = await eventsOf(result);
    const started = events.find((e) => e.type === "thread_started");
    expect(started?.data?.tools?.map((t) => t.name)).toEqual([
      "read_tool_result",
      "todo_write",
      "tool_search",
      "note",
      "mcp__docs__read_resource",
      "mcp__docs__search",
      "mcp__docs__send_email",
    ]);
    const previews = events.flatMap((e) =>
      e.type === "tool_result" ? [e.data?.preview] : [],
    );
    expect(previews[0]).toBe(
      "tool_not_loaded: mcp__docs__search; find it with tool_search first",
    );
    expect(previews[1]).toBe("mcp__docs__search: Search the docs.");
    expect(events.filter((e) => e.type === "tools_loaded")).toHaveLength(1);
    expect(calls.map((c) => c.tool)).toEqual(["search"]);
  });
});
