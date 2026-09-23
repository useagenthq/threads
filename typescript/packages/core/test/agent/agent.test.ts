import { describe, expect, test } from "bun:test";
import { z } from "zod";
import {
  agent,
  ConfigError,
  type Model,
  type RunResult,
  type StreamEvent,
  scriptedModel,
  sqlite,
  type ThreadRef,
  tool,
} from "../../src";
import { openStore } from "../../src/agent/sqlite";
import type { KnownEvent } from "../../src/log";
import { markTestKit } from "../../src/model/guard";
import { knownEvents } from "../../src/reduce";
import { refReader, verifyRequests } from "../../src/render";
import { unwrap } from "../store/helpers";

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

function threadOf<T>(result: RunResult<T>): ThreadRef {
  return result.thread;
}

/** The branch's events, with every recorded request re-verified (C7 and request_ref). */
async function logOf(thread: ThreadRef): Promise<readonly KnownEvent[]> {
  const { log, artifacts } = await openStore(thread.store);
  const events = knownEvents(unwrap(log.read(thread.branch)));
  unwrap(verifyRequests(events, refReader(artifacts)));
  return events;
}

const echo = tool({
  name: "echo",
  description: "Repeat the text.",
  input: z.object({ text: z.string() }),
  runs: "host",
  effect: "read_only",
  execute: async ({ text }) => text,
});

describe("the README example", () => {
  test("two lines: agent() and run()", async () => {
    const bot = agent({
      instructions: "You answer questions.",
      model: scriptedModel({ responses: [say("Hello!")] }),
    });
    const result = await bot.run("Hi", { store: sqlite(":memory:") });
    expect(result).toMatchObject({ status: "completed", output: "Hello!" });
  });
});

describe("tool arguments parse with the tool's own schema", () => {
  test("{text: 'hello'} runs; {text: 1} and extra keys fail before any effect", async () => {
    const model = scriptedModel({
      responses: [
        use("echo", { text: "hello" }, "c1"),
        use("echo", { text: 1 }, "c2"),
        use("echo", { text: "x", extra: true }, "c3"),
        say("done"),
      ],
    });
    const bot = agent({ model, tools: [echo] });
    const result = await bot.run("echo", { store: sqlite(":memory:") });
    expect(result.status).toBe("completed");
    const results = (await logOf(threadOf(result))).flatMap((e) =>
      e.type === "tool_result" ? [e.data] : [],
    );
    expect<unknown>(
      results.map((r) => [r.call_id, r.is_error, r.origin]),
    ).toEqual([
      ["c1", false, "executed"],
      ["c2", true, "not_executed"],
      ["c3", true, "not_executed"],
    ]);
    expect(results[0]?.preview).toBe("hello");
  });

  test("the model sees the exported JSON Schema, strict for objects", async () => {
    const bot = agent({
      model: scriptedModel({ responses: [say("ok")] }),
      tools: [echo],
    });
    const result = await bot.run("hi", { store: sqlite(":memory:") });
    const started = (await logOf(threadOf(result))).find(
      (e) => e.type === "thread_started",
    );
    // read_tool_result is always pinned first; echo follows it.
    expect(
      started?.type === "thread_started" &&
        started.data.tools.find((t) => t.name === "echo"),
    ).toEqual({
      name: "echo",
      description: "Repeat the text.",
      effect_class: "read_only",
      input_schema: {
        type: "object",
        properties: { text: { type: "string" } },
        required: ["text"],
        additionalProperties: false,
      },
    });
  });
});

describe("structured output", () => {
  test("a rejected candidate is retried; the accepted one is the typed output", async () => {
    const model = scriptedModel({
      responses: [
        use("final_output", { fixed: "yes" }, "c1"),
        use("final_output", { fixed: true }, "c2"),
      ],
    });
    const bot = agent({ model, output: z.object({ fixed: z.boolean() }) });
    const result = await bot.run("Is it fixed?", { store: sqlite(":memory:") });
    if (result.status !== "completed") throw new Error(result.status);
    const fixed: boolean = result.output.fixed;
    expect(fixed).toBe(true);
    const outcomes = (await logOf(result.thread)).flatMap((e) =>
      e.type === "output_validated" ? [e.data.outcome] : [],
    );
    expect(outcomes).toEqual(["rejected", "accepted"]);
  });
});

describe("stream()", () => {
  test("yields committed events and text deltas, then the result", async () => {
    const bot = agent({
      model: scriptedModel({ responses: [say("streamed")] }),
    });
    const run = bot.stream("hi", { store: sqlite(":memory:") });
    const items: StreamEvent[] = [];
    for await (const item of run) items.push(item);
    expect(items.flatMap((i) => (i.kind === "delta" ? [i.text] : []))).toEqual([
      "streamed",
    ]);
    const types = items.flatMap((i) =>
      i.kind === "event" ? [i.event.type] : [],
    );
    expect(types).toEqual([
      "user_input",
      "model_request",
      "model_response",
      "turn_completed",
    ]);
    expect((await run.result).status).toBe("completed");
  });
});

describe("the declared prefix is byte-equal within a settings epoch (invariant 5)", () => {
  test("every request of a multi-turn thread declares the same line 0", async () => {
    const model = scriptedModel({
      responses: [use("echo", { text: "a" }, "c1"), say("one"), say("two")],
    });
    const bot = agent({ model, tools: [echo] });
    const first = await bot.run("first", { store: sqlite(":memory:") });
    const second = await bot.run("second", { thread: first.thread });
    expect(second.status).toBe("completed");
    const prefixes = (await logOf(second.thread)).flatMap((e) =>
      e.type === "model_request"
        ? [JSON.stringify(e.data.declared_prefix)]
        : [],
    );
    expect(prefixes).toHaveLength(3);
    expect(new Set(prefixes).size).toBe(1);
  });

  test("a fallback starts a new epoch whose requests declare its own line 0", async () => {
    const overloaded = { error: { reason: "overloaded", http_status: 529 } };
    const bot = agent({
      model: scriptedModel({ responses: [overloaded, overloaded, overloaded] }),
      fallback: [scriptedSmall([say("from the fallback")])],
      retry: { base_delay_ms: 1, max_delay_ms: 1, fallback_after: 3 },
    });
    const result = await bot.run("hi", { store: sqlite(":memory:") });
    expect(result).toMatchObject({
      status: "completed",
      output: "from the fallback",
    });
    const events = await logOf(result.thread);
    const prefixes = events.flatMap((e) =>
      e.type === "model_request" ? [e.data.declared_prefix.sha256] : [],
    );
    expect(new Set(prefixes.slice(0, 3)).size).toBe(1);
    expect(prefixes[3]).not.toBe(prefixes[0]);
  });
});

/** A scripted model under another name, so policy.models lists two models. */
function scriptedSmall(responses: readonly unknown[]): Model {
  const inner = scriptedModel({ responses });
  const ref = { provider: "scripted", name: "scripted-small" };
  const small: Model = {
    ...inner,
    info: {
      ...inner.info,
      model: ref,
      limits: { ...inner.info.limits, ...ref },
    },
  };
  markTestKit(small);
  return small;
}

describe("permissions, parking and setup errors", () => {
  test("an undeclared tool is unguarded: in default mode it asks and the run parks", async () => {
    const send = tool({
      name: "send_email",
      description: "Send an email.",
      input: z.object({ to: z.string() }),
      runs: "host",
      execute: async () => "sent",
    });
    const bot = agent({
      model: scriptedModel({
        responses: [use("send_email", { to: "bob" }, "c1")],
      }),
      tools: [send],
    });
    const result = await bot.run("mail bob", { store: sqlite(":memory:") });
    expect(result).toMatchObject({
      status: "parked",
      reason: "awaiting_approval",
    });
  });

  test("two tools with one name are a setup error", async () => {
    const bot = agent({
      model: scriptedModel({ responses: [] }),
      tools: [echo, echo],
    });
    expect(await bot.check()).toMatchObject({
      ok: false,
      error: { code: "duplicate_name" },
    });
    expect(bot.run("hi", { store: sqlite(":memory:") })).rejects.toBeInstanceOf(
      ConfigError,
    );
  });
});

describe("the model-request guard", () => {
  test("a model that is not test kit is never sent a request in tests", async () => {
    const inner = scriptedModel({ responses: [say("never")] });
    const real: Model = { info: inner.info, send: inner.send };
    const bot = agent({ model: real });
    expect(bot.run("hi", { store: sqlite(":memory:") })).rejects.toThrow(
      "model request guard",
    );
  });
});
