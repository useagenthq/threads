import { describe, expect, test } from "bun:test";
import { z } from "zod";
import {
  agent,
  ConfigError,
  extension,
  fakeSandbox,
  scriptedModel,
  sqlite,
  tool,
} from "../../src";
import { openStore } from "../../src/agent/sqlite";
import { ObserverPump } from "../../src/hooks/observers";
import { BranchId, type KnownEvent } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";

// extension() through agent.run: instructions and namespaced tools in line 0, hooks bound to the
// run context, a pinned hook set (invariant 7), and observers that can't touch execution.

const usage = { input_tokens: 10, output_tokens: 2 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});

const ping = tool<Record<string, never>, string, { user: string }>({
  name: "ping",
  description: "Ping.",
  input: z.object({}),
  runs: "host",
  effect: "read_only",
  execute: async () => "pong",
});

async function logOf(store: ReturnType<typeof sqlite>, branch: string) {
  const { log } = await openStore(store);
  const read = unwrap(log.read(BranchId.parse(branch)));
  return knownEvents(read);
}

describe("extension()", () => {
  test("instructions and <ext>__<tool> go into line 0; hooks get the run context", async () => {
    const seen: string[] = [];
    const ops = extension<{ user: string }>({
      name: "ops",
      instructions: "Page on-call for outages.",
      tools: [ping],
      hooks: {
        sessionStart: async (source, ctx) => {
          seen.push(`${source}:${ctx.deps?.user}`);
          return ["On-call: alice."];
        },
      },
    });
    const bot = agent<{ user: string }>({
      instructions: "You run ops.",
      model: scriptedModel({ responses: [say("ok")] }),
      extensions: [ops],
    });
    const store = sqlite(":memory:");
    const result = await bot.run("hi", { store, deps: { user: "bob" } });
    expect(result.status).toBe("completed");
    expect(seen).toEqual(["startup:bob"]);
    const log = await logOf(store, result.thread.branch);
    const started = log[0];
    if (started?.type !== "thread_started")
      throw new Error("no thread_started");
    expect(started.data.instructions).toBe(
      "You run ops.\n\nPage on-call for outages.",
    );
    expect(started.data.tools.map((t) => t.name)).toContain("ops__ping");
    expect(log.map((e) => e.type).slice(1, 4)).toEqual([
      "hook_decision",
      "injected",
      "user_input",
    ]);
  });

  test("a bad name or timeout is a ConfigError", () => {
    expect(() => extension({ name: "has space" })).toThrow(ConfigError);
    expect(() => extension({ name: "x", hookTimeoutMs: 0 })).toThrow(
      ConfigError,
    );
  });

  test("a thread's hook set is pinned: another one is refused, not run", async () => {
    const guard = (hooks: Parameters<typeof extension>[0]["hooks"]) =>
      extension({ name: "guard", ...(hooks === undefined ? {} : { hooks }) });
    const store = sqlite(":memory:");
    const first = await agent({
      model: scriptedModel({ responses: [say("ok")] }),
      extensions: [
        guard({ beforeTool: async () => ({ decision: "deny", reason: "no" }) }),
      ],
    }).run("hi", { store });
    const without = agent({
      model: scriptedModel({ responses: [say("ok")] }),
      extensions: [guard(undefined)],
    });
    await expect(
      without.run("again", { store, thread: first.thread }),
    ).rejects.toThrow(ConfigError);
  });

  test("a resumed run on another sandbox provider is refused before any attach", async () => {
    const store = sqlite(":memory:");
    const first = await agent({
      model: scriptedModel({ responses: [say("ok")] }),
      sandbox: fakeSandbox(),
    }).run("hi", { store });
    const other = {
      ...fakeSandbox(),
      info: { ...fakeSandbox().info, provider: "other" },
    };
    let attached = 0;
    const counted = {
      ...other,
      attach: async (...args: Parameters<typeof other.attach>) => {
        attached += 1;
        return other.attach(...args);
      },
    };
    await expect(
      agent({
        model: scriptedModel({ responses: [say("ok")] }),
        sandbox: counted,
      }).run("again", { store, thread: first.thread }),
    ).rejects.toThrow(/sandbox provider fake, not other/);
    expect(attached).toBe(0);
  });

  test("setup runs once; a throw is a ConfigError from check()", async () => {
    let runs = 0;
    const ok = agent({
      model: scriptedModel({ responses: [] }),
      extensions: [extension({ name: "a", setup: async () => void runs++ })],
    });
    expect((await ok.check()).ok).toBe(true);
    expect((await ok.check()).ok).toBe(true);
    expect(runs).toBe(1);
    const bad = agent({
      model: scriptedModel({ responses: [] }),
      extensions: [
        extension({
          name: "b",
          setup: async () => {
            throw new Error("no creds");
          },
        }),
      ],
    });
    expect(await bad.check()).toMatchObject({
      ok: false,
      error: { code: "invalid_config" },
    });
  });
});

describe("observers (observer-failure-isolated)", () => {
  test("a throwing or stuck observer changes neither execution nor the log", async () => {
    const run = async (
      on?: Record<string, (e: KnownEvent) => Promise<void>>,
    ) => {
      const store = sqlite(":memory:");
      const result = await agent({
        model: scriptedModel({ responses: [say("ok")] }),
        ...(on === undefined
          ? {}
          : { extensions: [extension({ name: "o", on })] }),
      }).run("hi", { store });
      const log = await logOf(store, result.thread.branch);
      return { status: result.status, types: log.map((e) => e.type) };
    };
    const plain = await run();
    expect(
      await run({
        "*": async () => {
          throw new Error("exporter down");
        },
      }),
    ).toEqual(plain);
    expect(await run({ "*": () => new Promise<void>(() => {}) })).toEqual(
      plain,
    );
  });

  test("delivery is in order after append, resumes from its durable cursor, and retries a failure", async () => {
    const store = sqlite(":memory:");
    const { log } = await openStore(store);
    const result = await agent({
      model: scriptedModel({ responses: [say("ok")] }),
    }).run("hi", { store });
    const branch = result.thread.branch;
    const events = () => knownEvents(unwrap(log.read(branch)));
    let fail = true;
    const seen: number[] = [];
    const observer = {
      name: "audit",
      on: {
        "*": async (e: KnownEvent) => {
          if (e.seq === 3 && fail) throw new Error("once");
          seen.push(e.seq);
        },
      },
    };
    const pump = new ObserverPump(log.cursors, branch, events, [observer]);
    pump.poke();
    await pump.idle();
    expect(seen).toEqual([1, 2]);
    expect(unwrap(log.cursors.get("audit", branch))).toBe(2);
    fail = false;
    // A new pump (a restart) starts from the durable cursor, not from the beginning.
    const again = new ObserverPump(log.cursors, branch, events, [observer]);
    again.poke();
    await again.idle();
    expect(seen).toEqual([
      1,
      2,
      ...events()
        .slice(2)
        .map((e) => e.seq),
    ]);
    expect(unwrap(log.cursors.get("audit", branch))).toBe(
      events().at(-1)?.seq ?? 0,
    );
  });
});
