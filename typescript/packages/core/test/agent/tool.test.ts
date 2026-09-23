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

  test("runs sandbox is still capability_missing", () => {
    const sandboxed = tool({ ...base, runs: "sandbox" });
    expect(() => sandboxed.spec()).toThrow(ConfigError);
    try {
      sandboxed.spec();
    } catch (error) {
      expect(error instanceof ConfigError && error.code).toBe(
        "capability_missing",
      );
    }
    // @ts-expect-error runs only accepts "host" or "sandbox"
    tool({ ...base, runs: "elsewhere" });
  });
});
