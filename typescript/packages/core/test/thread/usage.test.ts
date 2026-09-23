import { describe, expect, test } from "bun:test";
import { agent, openThread, scriptedModel, sqlite } from "../../src";
import { openStore, storeConnection } from "../../src/agent/sqlite";
import { BranchId, ThreadId } from "../../src/log";
import { cost, knownEvents, reduce } from "../../src/reduce";
import { code, started, unwrap } from "../store/helpers";
import { configHash, corrupt, ONE, priced, run, say } from "./usage-kit";

// Thread.usage(), cost() and cacheBreaks() (spec/api.json) through openThread over real runs:
// each is the projection of one verified read, and a corrupt log is log_corrupt, never zero.

describe("Thread.usage", () => {
  test("is reduce().usage of the branch", async () => {
    const store = sqlite(":memory:");
    const thread = await run(store, priced([say("Hi.")]));
    const { log } = await openStore(store);
    const direct = reduce(unwrap(log.read(thread.branch)), log.now()).usage;
    expect(unwrap(await thread.usage())).toEqual(direct);
    expect(direct).toEqual({
      input_tokens: 10,
      output_tokens: 2,
      unknown_responses: 0,
    });
  });

  test("an unknown count is counted as unknown, never as zero", async () => {
    const store = sqlite(":memory:");
    const unknown = {
      ...say("Hi."),
      usage: { input_tokens: null, output_tokens: 2 },
    };
    const thread = await run(store, priced([unknown]));
    expect(unwrap(await thread.usage())).toEqual({
      input_tokens: 0,
      output_tokens: 2,
      unknown_responses: 1,
    });
  });
});

describe("Thread.cost", () => {
  test("a priced agent pins USD and costs the projection", async () => {
    const store = sqlite(":memory:");
    const thread = await run(store, priced([say("Hi.")]));
    const { log } = await openStore(store);
    const chain = unwrap(log.read(thread.branch));
    expect(chain.fold.policy?.currency).toBe("USD");
    const own = unwrap(await thread.cost());
    expect(own).toEqual(cost(knownEvents(chain), chain.fold.policy) ?? null);
    expect(own).toEqual({
      currency: "USD",
      known_nanos: ONE,
      upper_bound_nanos: ONE,
      complete: true,
      bounded: true,
    });
  });

  test("an unpriced model pins no currency, so cost is null", async () => {
    const store = sqlite(":memory:");
    const thread = await run(store, scriptedModel({ responses: [say("Hi.")] }));
    expect(unwrap(await thread.cost())).toBeNull();
    expect(unwrap(await thread.cost({ tree: true }))).toBeNull();
  });

  test("a priced thread pinned before currency reads, costs null and refuses to continue", async () => {
    // What an older agent() stored: this pin without policy.currency, hashed the same way.
    const fresh = await run(sqlite(":memory:"), priced([say("Hi.")]));
    const { log: freshLog } = await openStore(fresh.store);
    const [first] = knownEvents(unwrap(freshLog.read(fresh.branch)));
    if (first?.type !== "thread_started") throw new Error("no thread_started");
    const { config_hash, ...config } = first.data;
    // Recomputing the stored hash from the pin proves the formula the legacy hash uses.
    expect(configHash(config)).toBe(config_hash);
    const { currency: _currency, ...policy } = config.policy ?? {};
    const legacy = { ...config, policy };
    const store = sqlite(":memory:");
    const { log } = await openStore(store);
    const id = ThreadId.parse("0192a000-0000-7000-8000-0000000000bb");
    const branch = BranchId.parse("0192b000-0000-7000-8000-0000000000bb");
    unwrap(log.createBranch(id, branch));
    const writer = unwrap(log.acquire(branch, "older"));
    unwrap(
      writer.append([
        {
          type: "thread_started",
          type_version: 1,
          critical: true,
          actor: { kind: "host" },
          data: { ...legacy, config_hash: configHash(legacy) },
        },
      ]),
    );
    writer.release();
    const old = unwrap(await openThread(store, id));
    expect(unwrap(await old.cost())).toBeNull();
    const again = agent({ name: "lead", model: priced([say("More.")]) });
    await expect(again.run("More.", { thread: old })).rejects.toThrow(
      "another config",
    );
  });
});

describe("Thread.cacheBreaks", () => {
  test("is a list under the effective context policy", async () => {
    const store = sqlite(":memory:");
    const thread = await run(store, priced([say("Hi.")]));
    expect(unwrap(await thread.cacheBreaks())).toEqual([]);
  });
});

describe("usage, cost and cacheBreaks on edge logs", () => {
  test("a log with only thread_started: zero usage, no cost, no breaks", async () => {
    const store = sqlite(":memory:");
    const { log } = await openStore(store);
    const thread = ThreadId.parse("0192a000-0000-7000-8000-0000000000aa");
    const branch = BranchId.parse("0192b000-0000-7000-8000-0000000000aa");
    unwrap(log.createBranch(thread, branch));
    unwrap(unwrap(log.acquire(branch, "holder")).append([started]));
    const handle = unwrap(await openThread(store, thread));
    expect(unwrap(await handle.usage())).toEqual({
      input_tokens: 0,
      output_tokens: 0,
      unknown_responses: 0,
    });
    expect(unwrap(await handle.cost())).toBeNull();
    expect(unwrap(await handle.cacheBreaks())).toEqual([]);
  });

  test("a line from a newer writer is unsupported, not corrupt", async () => {
    const store = sqlite(":memory:");
    const thread = await run(store, priced([say("Hi.")]));
    const { db } = await storeConnection(store);
    db.run(
      'UPDATE events SET line = CAST(replace(CAST(line AS TEXT), \'"type":"turn_completed"\', \'"type":"approval_quorum"\') AS BLOB) WHERE branch_id = ?',
      [thread.branch],
    );
    expect([
      code(await thread.usage()),
      code(await thread.cost({ tree: true })),
      code(await thread.cacheBreaks()),
    ]).toEqual(Array(3).fill("unsupported_critical_event"));
  });

  test("a branch gone after open is log_corrupt, the declared error", async () => {
    const store = sqlite(":memory:");
    const thread = await run(store, priced([say("Hi.")]));
    const { db } = await storeConnection(store);
    // Another tenant's now: to this store the branch no longer exists.
    db.run("PRAGMA foreign_keys = OFF", []);
    db.run("UPDATE branches SET tenant_id = 'elsewhere' WHERE branch_id = ?", [
      thread.branch,
    ]);
    expect([
      code(await thread.usage()),
      code(await thread.cost()),
      code(await thread.cacheBreaks()),
    ]).toEqual(Array(3).fill("log_corrupt"));
  });

  test("a corrupt log is log_corrupt from every method, never zeros", async () => {
    const store = sqlite(":memory:");
    const thread = await run(store, priced([say("Hi.")]));
    await corrupt(store, thread.id, "Go.");
    expect(code(await thread.usage())).toBe("log_corrupt");
    expect(code(await thread.cost())).toBe("log_corrupt");
    expect(code(await thread.cost({ tree: true }))).toBe("log_corrupt");
    expect(code(await thread.cacheBreaks())).toBe("log_corrupt");
  });
});
