import { describe, expect, test } from "bun:test";
import { z } from "zod";
import {
  agent,
  ConfigError,
  type Model,
  openThread,
  scriptedModel,
  sqlite,
  tool,
} from "../../src";
import { credential } from "../../src/agent/secret";
import { redactSecrets } from "../../src/redact";

// Lane 09: setup is result-valued at check(), thrown by a run, remembered per object on success
// only, shared by a parent and the agents it may start; credentials are redacted longest first.

const usage = { input_tokens: 1, output_tokens: 1 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});

/** A scripted model whose setup is counted and fails while `failing()` says so. */
function counted(
  responses: readonly unknown[],
  failing: () => boolean = () => false,
): { readonly model: Model; readonly calls: () => number } {
  let calls = 0;
  // Added in place: the model-request guard knows the scripted model by identity.
  const model: Model = Object.assign(
    scriptedModel({ responses: [...responses] }),
    {
      setup: async () => {
        calls += 1;
        if (failing())
          throw new ConfigError(
            "missing_secret",
            "fake: set apiKey or FAKE_KEY",
          );
      },
    },
  );
  return { model, calls: () => calls };
}

describe("setup", () => {
  test("check() returns a failure as a value; the run throws the same ConfigError", async () => {
    const { model } = counted([], () => true);
    const bot = agent({ model });
    expect(await bot.check()).toEqual({
      ok: false,
      error: {
        code: "missing_secret",
        message: "fake: set apiKey or FAKE_KEY",
      },
    });
    await expect(
      bot.run("hi", { store: sqlite(":memory:") }),
    ).rejects.toMatchObject({ code: "missing_secret" });
  });

  test("a failed setup is retried on the same agent; a successful one is not repeated", async () => {
    let broken = true;
    const { model, calls } = counted([say("one"), say("two")], () => broken);
    const bot = agent({ model });
    expect((await bot.check()).ok).toBe(false);
    broken = false;
    expect(await bot.check()).toEqual({ ok: true, value: undefined });
    const store = sqlite(":memory:");
    expect((await bot.run("a", { store })).status).toBe("completed");
    expect((await bot.run("b", { store })).status).toBe("completed");
    expect(calls()).toBe(2);
  });

  test("a model shared by a parent and its subagent is set up once", async () => {
    const { model, calls } = counted([]);
    const child = agent({ name: "child", model });
    const parent = agent({ name: "parent", model, subagents: [child] });
    expect((await parent.check()).ok).toBe(true);
    expect((await child.check()).ok).toBe(true);
    expect(calls()).toBe(1);
  });

  test("a parent's check() fails on its subagent's setup", async () => {
    const { model: broken } = counted([], () => true);
    const child = agent({ name: "child", model: broken });
    const parent = agent({
      name: "parent",
      model: scriptedModel({ responses: [] }),
      subagents: [child],
    });
    expect(await parent.check()).toMatchObject({
      ok: false,
      error: { code: "missing_secret" },
    });
  });

  test("check() is a result, not void", async () => {
    const bot = agent({ model: scriptedModel({ responses: [] }) });
    // @ts-expect-error check() returns a result value, never void or undefined
    const nothing: undefined = await bot.check();
    expect(JSON.stringify(nothing)).toBe('{"ok":true}');
  });
});

describe("credential() and redaction (C5)", () => {
  test("missing or empty names the option and the variable", () => {
    delete process.env["THREADS_TEST_CRED_UNSET"];
    for (const value of [undefined, ""])
      expect(() =>
        credential("fake", "apiKey", value, "THREADS_TEST_CRED_UNSET")(),
      ).toThrow("fake: set apiKey or THREADS_TEST_CRED_UNSET");
  });

  test("a longer value is replaced whole even when a prefix was registered first", () => {
    credential("short", "apiKey", "abc-lane09", "UNUSED")();
    credential("long", "apiKey", "abc-lane09-123", "UNUSED")();
    expect(redactSecrets("x abc-lane09-123 y abc-lane09")).toBe(
      "x [secret long.apiKey] y [secret short.apiKey]",
    );
  });

  test("one value under two labels redacts to the smaller label", () => {
    credential("zeta", "apiKey", "same-lane09-value", "UNUSED")();
    credential("alpha", "apiKey", "same-lane09-value", "UNUSED")();
    expect(redactSecrets("same-lane09-value")).toBe("[secret alpha.apiKey]");
  });
});

describe("lane 09 review fixes", () => {
  test("a read_only tool that echoes a key never stores or sends it", async () => {
    const key = credential("fake", "apiKey", "sk-lane09-read-only-4c1f", "U")();
    const peek = tool({
      name: "peek",
      description: "Peek.",
      input: z.object({}),
      effect: "read_only",
      execute: async () => `key=${key}`,
    });
    const sent: Uint8Array[] = [];
    const model = scriptedModel({
      responses: [
        {
          content: [
            { type: "tool_use", call_id: "c1", name: "peek", input: {} },
          ],
          stop_reason: "tool_use",
          usage,
        },
        say("ok"),
      ],
    });
    const send = model.send;
    // Kept in place: the model-request guard knows the scripted model by identity.
    Object.assign(model, {
      send: (...args: Parameters<Model["send"]>) => {
        sent.push(args[0].body);
        return send(...args);
      },
    });
    const store = sqlite(":memory:");
    const result = await agent({
      model,
      tools: [peek],
      permissions: { mode: "bypass" },
    }).run("go", { store });
    const thread = await openThread(store, result.thread.id);
    if (!thread.ok) throw new Error(thread.error.message);
    const timeline = await thread.value.timeline();
    if (!timeline.ok) throw new Error(timeline.error.message);
    const stored = JSON.stringify(timeline.value.entries);
    expect(stored).toContain("[secret fake.apiKey]");
    expect(stored).not.toContain(key);
    const requests = sent.map((b) => new TextDecoder().decode(b));
    expect(requests.length).toBe(2);
    for (const r of requests) expect(r).not.toContain(key);
  });

  test("a credential keeps the value it resolved; a failure is resolved again", () => {
    process.env["THREADS_TEST_KEPT"] = "kept-lane09-value";
    const kept = credential("fake", "apiKey", undefined, "THREADS_TEST_KEPT");
    expect(kept()).toBe("kept-lane09-value");
    delete process.env["THREADS_TEST_KEPT"];
    expect(kept()).toBe("kept-lane09-value");
    const late = credential("fake", "apiKey", undefined, "THREADS_TEST_LATE");
    expect(() => late()).toThrow(ConfigError);
    process.env["THREADS_TEST_LATE"] = "late-lane09-value";
    expect(late()).toBe("late-lane09-value");
    delete process.env["THREADS_TEST_LATE"];
  });

  test("concurrent checks share one setup, its failure included; the next check retries", async () => {
    let broken = true;
    let calls = 0;
    const model: Model = Object.assign(scriptedModel({ responses: [] }), {
      setup: async () => {
        calls += 1;
        await Bun.sleep(10);
        if (broken)
          throw new ConfigError(
            "missing_secret",
            "fake: set apiKey or FAKE_KEY",
          );
      },
    });
    const bot = agent({ model });
    const failed = await Promise.all([bot.check(), bot.check()]);
    expect(failed.map((c) => c.ok)).toEqual([false, false]);
    expect(calls).toBe(1);
    broken = false;
    const both = await Promise.all([bot.check(), bot.check()]);
    expect(both.map((c) => c.ok)).toEqual([true, true]);
    expect(calls).toBe(2);
  });
});
