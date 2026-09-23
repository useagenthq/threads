import { describe, expect, test } from "bun:test";
import { z } from "zod";
import {
  agent,
  ConfigError,
  scriptedModel,
  sqlite,
  type ThreadRef,
  tool,
} from "../../src";
import { openStore } from "../../src/agent/sqlite";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";

const usage = { input_tokens: 10, output_tokens: 2 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
const use = (text: string) => ({
  content: [{ type: "tool_use", call_id: "c1", name: "echo", input: { text } }],
  stop_reason: "tool_use",
  usage,
});

const base = {
  name: "echo",
  description: "Repeat the text.",
  input: z.object({ text: z.string() }),
  effect: "read_only",
  execute: async ({ text }: { readonly text: string }) => text,
} as const;

async function configHash(thread: ThreadRef): Promise<string | undefined> {
  const { log } = await openStore(thread.store);
  const started = knownEvents(unwrap(log.read(thread.branch)))[0];
  return started?.type === "thread_started"
    ? started.data.config_hash
    : undefined;
}

describe("tool() runs", () => {
  test("a tool without runs is a host tool", async () => {
    const bot = agent({
      model: scriptedModel({ responses: [use("hello"), say("done")] }),
      tools: [tool(base)],
    });
    const result = await bot.run("echo", { store: sqlite(":memory:") });
    expect(result).toMatchObject({ status: "completed", output: "done" });
  });

  test("omitted and explicit host runs pin identical bytes", async () => {
    const omitted = tool(base);
    const host = tool({ ...base, runs: "host" });
    expect(omitted.spec()).toEqual(host.spec());
    const hashOf = async (t: typeof omitted): Promise<string | undefined> => {
      const bot = agent({
        model: scriptedModel({ responses: [say("ok")] }),
        tools: [t],
      });
      const result = await bot.run("hi", { store: sqlite(":memory:") });
      return configHash(result.thread);
    };
    const hash = await hashOf(omitted);
    expect(hash).toBeString();
    expect(hash).toBe(await hashOf(host));
  });

  test("runs sandbox is reserved: setup fails and execute never runs", async () => {
    let executed = false;
    const sandboxed = tool({
      ...base,
      runs: "sandbox",
      execute: async ({ text }) => {
        executed = true;
        return text;
      },
    });
    const bot = agent({
      model: scriptedModel({ responses: [use("hello"), say("done")] }),
      tools: [sandboxed],
    });
    expect(await bot.check()).toMatchObject({
      ok: false,
      error: { code: "capability_missing" },
    });
    await expect(
      bot.run("echo", { store: sqlite(":memory:") }),
    ).rejects.toThrow(ConfigError);
    expect(executed).toBe(false);
    // @ts-expect-error runs only accepts "host" or "sandbox"
    tool({ ...base, runs: "elsewhere" });
  });
});

describe("tool() concurrent", () => {
  const { effect: _readOnly, ...unsaid } = base;
  const hashOf = async (t: ReturnType<typeof tool>) => {
    const bot = agent({
      model: scriptedModel({ responses: [say("ok")] }),
      tools: [t],
    });
    const result = await bot.run("hi", { store: sqlite(":memory:") });
    return configHash(result.thread);
  };

  test.each([
    ["an omitted effect (unguarded)", tool({ ...unsaid, concurrent: true })],
    ["unguarded", tool({ ...base, effect: "unguarded", concurrent: true })],
    [
      "idempotent",
      tool({
        ...base,
        effect: "idempotent",
        dedupWindowMs: 1000,
        concurrent: true,
      }),
    ],
    [
      "reconcilable",
      tool({
        ...base,
        effect: "reconcilable",
        reconcile: {
          lookup: async () => ({ status: "not_found" }),
          finality: "final",
        },
        concurrent: true,
      }),
    ],
    ["ends_turn", tool({ ...base, endsTurn: true, concurrent: true })],
  ])("concurrent with %s is invalid_config naming the tool", (_, t) => {
    expect(() => t.spec()).toThrow(ConfigError);
    expect(() => t.spec()).toThrow("tool echo: concurrent");
  });

  test("the ToolSpec equals the one without concurrent: the model never sees it", () => {
    expect(tool({ ...base, concurrent: true }).spec()).toEqual(
      tool(base).spec(),
    );
  });

  test("config_hash covers it; no concurrent tool keeps the hash", async () => {
    const plain = await hashOf(tool(base));
    expect(await hashOf(tool({ ...base, concurrent: false }))).toBe(plain);
    expect(await hashOf(tool({ ...base, concurrent: true }))).not.toBe(plain);
    expect({
      plain,
      concurrent: await hashOf(tool({ ...base, concurrent: true })),
    }).toEqual({
      plain: "be791e242b20a14b825d5bd99584c51bd4e8b4d5bdffd37fb10590010bf5cec9",
      concurrent:
        "d2277d2fc7926e8c9ae5676a3dda3cf00e1147e35b032ada444160201d402c39",
    });
  });

  test("turning it on for an existing thread fails closed", async () => {
    const say1 = () => scriptedModel({ responses: [say("ok")] });
    const seed = await agent({ model: say1(), tools: [tool(base)] }).run("hi", {
      store: sqlite(":memory:"),
    });
    const flipped = agent({
      model: say1(),
      tools: [tool({ ...base, concurrent: true })],
    });
    await expect(flipped.run("again", { thread: seed.thread })).rejects.toThrow(
      "another config",
    );
  });
});
