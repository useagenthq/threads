import { describe, expect, test } from "bun:test";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { agent, openThread, scriptedModel, sqlite, tool } from "@threads/core";
import { ThreadId } from "@threads/core/host";
import { z } from "zod";
import { mcp } from "../src";
import { eventsOf, P, say, use } from "./kit";

// Lane 09: every check() and every run opens its own MCP session and closes it before it
// returns, however it ends. The stdio server is one process per connection and logs when each
// starts and ends, so the count of open connections is read from the server's side.

function local() {
  const dir = mkdtempSync(join(tmpdir(), "threads-mcp-life-"));
  const connections = join(dir, "connections");
  const server = mcp({
    name: "local",
    command: process.execPath,
    args: [join(import.meta.dir, "server.ts")],
    env: { CONNECTIONS_FILE: connections, CALLS_FILE: join(dir, "calls") },
  });
  const lines = (): string[] =>
    existsSync(connections)
      ? readFileSync(connections, "utf8").split("\n")
      : [];
  return {
    server,
    open: () =>
      lines().filter((l) => l === "open").length -
      lines().filter((l) => l === "closed").length,
    opened: () => lines().filter((l) => l === "open").length,
  };
}

const noop = (name: string) =>
  tool({
    name,
    description: "Does nothing.",
    input: z.object({}),
    effect: "read_only",
    execute: async () => "ok",
  });

describe("MCP sessions are scoped to one check() or one run", () => {
  test("check() leaves no connection open, on success and on failure", async () => {
    const s = local();
    const ok = agent({
      model: scriptedModel({ responses: [] }),
      tools: [s.server],
    });
    expect(await ok.check()).toEqual({ ok: true, value: undefined });
    expect([s.opened(), s.open()]).toEqual([1, 0]);
    const clash = agent({
      model: scriptedModel({ responses: [] }),
      tools: [noop("mcp__local__search"), s.server],
    });
    expect(await clash.check()).toMatchObject({
      ok: false,
      error: { code: "duplicate_name" },
    });
    expect([s.opened(), s.open()]).toEqual([2, 0]);
  });

  test("a run calls through its own session and closes it when it ends", async () => {
    const s = local();
    const bot = agent({
      model: scriptedModel({
        responses: [
          use("mcp__local__search", { query: "x" }, "c1"),
          say("done"),
        ],
      }),
      tools: [s.server],
      permissions: { mode: "bypass" },
    });
    expect((await bot.check()).ok).toBe(true);
    const result = await bot.run("find", { store: sqlite(":memory:") });
    expect(result.status).toBe("completed");
    const shown = (await eventsOf(result)).find(
      (e) => e.type === "tool_result",
    );
    expect(shown?.data?.preview).toContain("results for x");
    expect([s.opened(), s.open()]).toEqual([2, 0]);
  });

  test("a cancelled run closes its session", async () => {
    const s = local();
    const store = sqlite(":memory:");
    // The operator cancels the thread while the run is in its tool call.
    const stop = tool({
      name: "stop",
      description: "Cancels the run.",
      input: z.object({}),
      effect: "read_only",
      execute: async (_input, ctx) => {
        const thread = await openThread(store, ThreadId.parse(ctx.threadId));
        if (!thread.ok) throw new Error(thread.error.message);
        await thread.value.cancel(P);
        return "stopping";
      },
    });
    const bot = agent({
      model: scriptedModel({
        responses: [use("stop", {}, "c1"), say("never")],
      }),
      tools: [stop, s.server],
      permissions: { mode: "bypass" },
    });
    const result = await bot.run("go", { store });
    expect(result.status).toBe("cancelled");
    expect([s.opened(), s.open()]).toEqual([1, 0]);
  });

  test("two concurrent runs of one agent hold two sessions", async () => {
    const s = local();
    const both = Promise.withResolvers<void>();
    const seen: number[] = [];
    const meet = tool({
      name: "meet",
      description: "Waits for the other run.",
      input: z.object({}),
      effect: "read_only",
      execute: async () => {
        seen.push(s.open());
        if (seen.length === 2) both.resolve();
        await both.promise;
        return "met";
      },
    });
    const bot = agent({
      model: scriptedModel({
        responses: [
          use("meet", {}, "c1"),
          use("meet", {}, "c2"),
          say("one"),
          say("two"),
        ],
      }),
      tools: [meet, s.server],
      permissions: { mode: "bypass" },
    });
    const runs = await Promise.all([
      bot.run("a", { store: sqlite(":memory:") }),
      bot.run("b", { store: sqlite(":memory:") }),
    ]);
    expect(runs.map((r) => r.status)).toEqual(["completed", "completed"]);
    expect(seen.at(-1)).toBe(2);
    expect(s.open()).toBe(0);
  });

  test("an unreachable server fails check() as a value and the run as a ConfigError", async () => {
    const bot = agent({
      model: scriptedModel({ responses: [] }),
      tools: [mcp({ name: "gone", command: "/nonexistent/threads-mcp" })],
    });
    expect(await bot.check()).toMatchObject({
      ok: false,
      error: { code: "mcp_unreachable" },
    });
    await expect(
      bot.run("go", { store: sqlite(":memory:") }),
    ).rejects.toMatchObject({ code: "mcp_unreachable" });
  });
});
