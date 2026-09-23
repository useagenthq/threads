import { afterEach, describe, expect, jest, test } from "bun:test";
import { z } from "zod";
import {
  agent,
  LEASE_TTL_MS,
  LogStore,
  type Model,
  type ModelChunk,
  scriptedModel,
  sqlite,
  tool,
} from "../../src";
import { storeOf } from "../../src/agent/sqlite";
import { markTestKit } from "../../src/model/guard";
import { knownEvents } from "../../src/reduce";
import { memoryArtifacts } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { unwrap } from "../store/helpers";

// each run owns the branch lease on its own, keeps it through slow awaits and hands
// it back when it ends.

const usage = { input_tokens: 10, output_tokens: 2 };
const info = scriptedModel({ responses: [] }).info;

/** A test-kit model that answers `ok <n>`, after `wait` resolves if one is given. */
function model(wait?: (n: number) => Promise<void>): Model {
  let sends = 0;
  const made: Model = {
    info,
    async *send(_request, ctx): AsyncIterable<ModelChunk> {
      sends += 1;
      await wait?.(sends);
      const fenced = await ctx.fence();
      if (!fenced.ok) {
        yield { kind: "rejected", reason: "stale_epoch" };
        return;
      }
      yield { kind: "part", part: { type: "text", text: `ok ${sends}` } };
      yield { kind: "done", stop_reason: "end_turn", usage };
    },
  };
  markTestKit(made);
  return made;
}

afterEach(() => {
  jest.useRealTimers();
});

describe("run ownership of the branch lease", () => {
  test("a second run on a busy branch is refused and the first completes", async () => {
    const entered = Promise.withResolvers<void>();
    const release = Promise.withResolvers<void>();
    const bot = agent({
      model: model(async (n) => {
        if (n !== 2) return;
        entered.resolve();
        await release.promise;
      }),
    });
    const seed = await bot.run("seed", { store: sqlite(":memory:") });
    const first = bot.run("first", { thread: seed.thread });
    await entered.promise;
    const second = await bot.run("overlap", { thread: seed.thread });
    release.resolve();
    expect(second.status).toBe("failed");
    if (second.status === "failed")
      expect(second.error.code).toBe("branch_busy");
    expect((await first).status).toBe("completed");
  });

  test("a model call longer than the lease TTL still commits", async () => {
    jest.useFakeTimers();
    const artifacts = memoryArtifacts();
    const log = unwrap(
      LogStore.open(openBunSqlite(":memory:"), () => Date.now(), artifacts),
    );
    const store = storeOf({ log, artifacts });
    const bot = agent({
      model: model(async () => {
        const { promise, resolve } = Promise.withResolvers<void>();
        setTimeout(resolve, LEASE_TTL_MS + 1_000);
        await promise;
      }),
    });
    const pending = bot.run("slow", { store });
    let done = false;
    void pending.finally(() => {
      done = true;
    });
    for (let i = 0; i < 100 && !done; i += 1) {
      for (let k = 0; k < 50; k += 1) await Promise.resolve();
      jest.advanceTimersByTime(1_000);
    }
    expect((await pending).status).toBe("completed");
  });

  test("a tool call longer than the lease TTL still commits", async () => {
    jest.useFakeTimers();
    const artifacts = memoryArtifacts();
    const log = unwrap(
      LogStore.open(openBunSqlite(":memory:"), () => Date.now(), artifacts),
    );
    const slow = tool({
      name: "slow",
      description: "Wait, then answer.",
      input: z.object({}),
      runs: "host",
      effect: "read_only",
      execute: async () => {
        const { promise, resolve } = Promise.withResolvers<void>();
        setTimeout(resolve, LEASE_TTL_MS + 1_000);
        await promise;
        return "waited";
      },
    });
    const bot = agent({
      model: scriptedModel({
        responses: [
          {
            content: [
              { type: "tool_use", call_id: "c1", name: "slow", input: {} },
            ],
            stop_reason: "tool_use",
            usage,
          },
          {
            content: [{ type: "text", text: "done" }],
            stop_reason: "end_turn",
            usage,
          },
        ],
      }),
      tools: [slow],
    });
    const pending = bot.run("go", { store: storeOf({ log, artifacts }) });
    let done = false;
    void pending.finally(() => {
      done = true;
    });
    for (let i = 0; i < 100 && !done; i += 1) {
      for (let k = 0; k < 50; k += 1) await Promise.resolve();
      jest.advanceTimersByTime(1_000);
    }
    expect((await pending).status).toBe("completed");
  });

  test("a finished run hands the lease back, so the next run starts at once", async () => {
    const bot = agent({ model: model() });
    const one = await bot.run("one", { store: sqlite(":memory:") });
    const two = await bot.run("two", { thread: one.thread });
    expect(two.status).toBe("completed");
  });

  test("a run that throws still hands the lease back", async () => {
    const store = sqlite(":memory:");
    const bot = agent({ instructions: "one", model: model() });
    const seed = await bot.run("seed", { store });
    // Continuing with another config throws ConfigError after the lease is taken.
    const other = agent({ instructions: "two", model: model() });
    await expect(other.run("boom", { thread: seed.thread })).rejects.toThrow(
      "another config",
    );
    const next = await bot.run("after", { thread: seed.thread });
    expect(next.status).toBe("completed");
  });

  test("a lost lease fences the run: nothing it received is recorded", async () => {
    jest.useFakeTimers();
    const artifacts = memoryArtifacts();
    const log = unwrap(
      LogStore.open(openBunSqlite(":memory:"), () => Date.now(), artifacts),
    );
    const store = storeOf({ log, artifacts });
    const seed = await agent({ model: model() }).run("seed", { store });
    const bot = agent({
      model: model(async () => {
        // A stalled process: the clock jumps past the TTL with no renewal, and another
        // executor takes the branch.
        jest.setSystemTime(Date.now() + LEASE_TTL_MS + 1);
        unwrap(log.acquire(seed.thread.branch, "intruder"));
      }),
    });
    const lost = await bot.run("late", { thread: seed.thread });
    expect(lost.status).toBe("failed");
    const events = knownEvents(unwrap(log.read(seed.thread.branch)));
    expect(events.filter((e) => e.type === "model_response")).toHaveLength(1);
  });
});
