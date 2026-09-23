import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, scriptedModel, sqlite, type ThreadRef, tool } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import type { KnownEvent } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";

// Budgets are tree-wide: each covers its thread and every
// descendant, every attempt reserves its bound first, and a refusal records budget_exceeded,
// ends the turn budget_exhausted and sends nothing more.

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
const echo = tool({
  name: "echo",
  description: "Repeat the text.",
  input: z.object({ text: z.string() }),
  runs: "host",
  effect: "read_only",
  execute: async ({ text }) => text,
});
const spawn = use("spawn_agent", { agent: "worker", prompt: "Work." }, "s1");

type Store = ReturnType<typeof sqlite>;

async function events(
  store: Store,
  thread: ThreadRef,
): Promise<readonly KnownEvent[]> {
  const { log } = await openStore(store);
  return knownEvents(unwrap(log.read(thread.branch)));
}

async function childEvents(
  store: Store,
  parent: readonly KnownEvent[],
): Promise<readonly KnownEvent[]> {
  const spawned = parent.find((e) => e.type === "agent_spawned");
  if (spawned?.type !== "agent_spawned") throw new Error("no child");
  const { log } = await openStore(store);
  const branch = unwrap(log.mainBranch(spawned.data.child_thread_id));
  return knownEvents(unwrap(log.read(branch)));
}

const exceeded = (log: readonly KnownEvent[]) =>
  log.flatMap((e) => (e.type === "budget_exceeded" ? [e.data] : []));

describe("budgets", () => {
  test("a thread budget refuses the request past its limit; nothing is sent after", async () => {
    const store = sqlite(":memory:");
    const model = scriptedModel({
      responses: [
        use("echo", { text: "a" }, "c1"),
        use("echo", { text: "b" }, "c2"),
      ],
    });
    const bot = agent({
      model,
      tools: [echo],
      budget: { max_model_requests: 2 },
    });
    const result = await bot.run("go", { store });
    expect(result).toMatchObject({
      status: "budget_exhausted",
      budget: {
        scope: "thread",
        limit: "max_model_requests",
        limit_value: 2,
        observed: 3,
      },
    });
    const log = await events(store, result.thread);
    expect(log.filter((e) => e.type === "model_request")).toHaveLength(2);
    expect(log.at(-1)?.type === "turn_completed" && log.at(-1)).toMatchObject({
      data: { reason: "budget_exhausted" },
    });
    expect(model.unexpected()).toBe(0);
  });

  test("a child that hits its own budget stops budget_exhausted; the parent goes on (F7.7)", async () => {
    const store = sqlite(":memory:");
    const worker = agent({
      name: "worker",
      model: scriptedModel({ responses: [use("echo", { text: "a" }, "w1")] }),
      tools: [echo],
      budget: { max_model_requests: 1 },
    });
    const lead = agent({
      model: scriptedModel({ responses: [spawn, say("The worker ran out.")] }),
      tools: [echo],
      subagents: [worker],
    });
    const result = await lead.run("go", { store });
    expect(result).toMatchObject({
      status: "completed",
      output: "The worker ran out.",
    });
    const log = await events(store, result.thread);
    const finished = log.find((e) => e.type === "agent_finished");
    expect(finished?.type === "agent_finished" && finished.data.status).toBe(
      "budget_exhausted",
    );
    expect(exceeded(await childEvents(store, log))).toEqual([
      {
        scope: "thread",
        limit: "max_model_requests",
        limit_value: 1,
        observed: 2,
        observed_is_upper_bound: false,
      },
    ]);
  });

  test("a parent's budget covers its child: the child is refused as ancestor-owned", async () => {
    const store = sqlite(":memory:");
    const worker = agent({
      name: "worker",
      model: scriptedModel({
        responses: [
          use("echo", { text: "a" }, "w1"),
          use("echo", { text: "b" }, "w2"),
        ],
      }),
      tools: [echo],
    });
    const lead = agent({
      model: scriptedModel({ responses: [spawn] }),
      tools: [echo],
      subagents: [worker],
      budget: { max_model_requests: 3 },
    });
    const result = await lead.run("go", { store });
    // lead 1 + worker 2 = 3; the worker's third request would be the tree's fourth.
    const log = await events(store, result.thread);
    expect(exceeded(await childEvents(store, log))).toEqual([
      {
        scope: "ancestor",
        owner_thread_id: result.thread.id,
        limit: "max_model_requests",
        limit_value: 3,
        observed: 4,
        observed_is_upper_bound: false,
      },
    ]);
    // The lead is covered by the same budget, so its next request is refused too.
    expect(result).toMatchObject({
      status: "budget_exhausted",
      budget: { scope: "thread", observed: 4 },
    });
  });
});
