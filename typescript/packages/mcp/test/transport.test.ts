import { describe, expect, test } from "bun:test";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { agent, scriptedModel, secret, sqlite } from "@threads/core";
import { mcp } from "../src";
import { bound, ctx, eventsOf, handles, say, server, use } from "./kit";
import type { Call } from "./server";

// The fence at the real transport (HTTP fetch, stdio writes) and credentials that stay on the
// host (C5).

describe("the fence at the real transport", () => {
  test("HTTP: a stale lease sends nothing; the run is not_sent", async () => {
    const calls: Call[] = [];
    const impl = await bound(server({ calls }), "mcp__docs__search");
    expect(await impl.run({ query: "x" }, ctx(true))).toEqual({
      kind: "not_sent",
    });
    expect(calls).toEqual([]);
  });

  test("stdio: the server runs on the host; a stale lease writes nothing to it", async () => {
    const dir = mkdtempSync(join(tmpdir(), "threads-mcp-"));
    const file = join(dir, "calls");
    const h = mcp({
      name: "local",
      command: process.execPath,
      args: [join(import.meta.dir, "server.ts")],
      env: { CALLS_FILE: file },
    });
    handles.push(h);
    const impl = await bound(h, "mcp__local__search");
    expect(await impl.run({ query: "x" }, ctx(true))).toEqual({
      kind: "not_sent",
    });
    expect(existsSync(file)).toBe(false);
    const ran = await impl.run({ query: "hello" }, ctx());
    expect(ran).toMatchObject({ kind: "done", isError: false });
    expect(readFileSync(file, "utf8")).toBe("search\n");
  });
});

describe("credentials stay on the host (C5)", () => {
  test("a secret() header reaches the server and never the log", async () => {
    process.env["THREADS_TEST_MCP_TOKEN"] = "tok-123-secret";
    const calls: Call[] = [];
    const bot = agent({
      model: scriptedModel({
        responses: [
          use("mcp__docs__search", { query: "x" }, "c1"),
          say("done"),
        ],
      }),
      tools: [
        server({
          calls,
          headers: { Authorization: secret("THREADS_TEST_MCP_TOKEN") },
        }),
      ],
      permissions: { mode: "bypass" },
    });
    const result = await bot.run("find", { store: sqlite(":memory:") });
    expect(calls[0]?.authorization).toBe("tok-123-secret");
    expect(JSON.stringify(await eventsOf(result))).not.toContain(
      "tok-123-secret",
    );
    delete process.env["THREADS_TEST_MCP_TOKEN"];
  });

  test("runs: sandbox is refused at setup, never started with host credentials", async () => {
    const checked = await agent({
      model: scriptedModel({ responses: [] }),
      tools: [mcp({ name: "boxed", command: "x", runs: "sandbox" })],
    }).check();
    expect(checked).toMatchObject({
      ok: false,
      error: { code: "capability_missing" },
    });
  });
});
