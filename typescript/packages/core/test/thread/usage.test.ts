import { describe, expect, test } from "bun:test";
import { agent, openThread, scriptedModel, sqlite } from "../../src";
import { openStore, storeConnection } from "../../src/agent/sqlite";
import { BranchId, ThreadId } from "../../src/log";
import { cost, knownEvents, reduce } from "../../src/reduce";
import type { CostError, ReadError } from "../../src/thread";
import { code, unwrap } from "../store/helpers";
import { obj, rewriteLog } from "./rewrite-log";
import { configHash, corrupt, ONE, priced, run, say, spawn } from "./usage-kit";

// The error types are exactly the codes spec/api.json declares: a compile error otherwise.
type Same<A, B> = [A] extends [B] ? ([B] extends [A] ? true : false) : false;
type Read = "log_corrupt" | "unsupported_format" | "unsupported_critical_event";
const readCodes: Same<ReadError["code"], Read> = true;
const costCodes: Same<CostError["code"], Read | "cost_overflow"> = true;
void [readCodes, costCodes];

// Thread.usage(), cost() and cacheBreaks() (spec/api.json) through openThread over real runs:
// each is the projection of one verified read, and a corrupt log is log_corrupt, never zero.

describe("Thread.usage", () => {
  test("is reduce().usage of the branch", async () => {
    const store = sqlite(":memory:");
    const thread = await run(store, priced([say("Hi.")]));
    const { log } = await openStore(store);
    const direct = reduce(
      unwrap(await log.read(thread.branch)),
      log.now(),
    ).usage;
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

describe("usage totals stay wire integers", () => {
  test("a response that would take a total past 2^53 - 1 counts as unknown", async () => {
    const store = sqlite(":memory:");
    const kid = agent({
      name: "kid",
      model: scriptedModel({ responses: [say("Kid.")] }),
    });
    const lead = scriptedModel({ responses: [spawn("kid"), say("Done.")] });
    const thread = await run(store, lead, [kid]);
    // As a provider reporting absurd counts would have recorded them: each response over half
    // the range, so the second would take the input total past it.
    const half = Math.floor(Number.MAX_SAFE_INTEGER / 2) + 1;
    const big = { input_tokens: half, output_tokens: 2 };
    await rewriteLog(store, thread.id, (lines) =>
      lines.map((l) =>
        l["type"] === "model_response"
          ? { ...l, data: { ...obj(l["data"]), usage: big } }
          : l,
      ),
    );
    expect(unwrap(await thread.usage())).toEqual({
      input_tokens: half,
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
    const chain = unwrap(await log.read(thread.branch));
    expect(chain.fold.policy?.currency).toBe("USD");
    const own = unwrap(await thread.cost());
    expect<unknown>(own).toEqual(cost(knownEvents(chain), chain.fold.policy));
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
    const [first] = knownEvents(unwrap(await freshLog.read(fresh.branch)));
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
    unwrap(await log.createBranch(id, branch));
    const writer = unwrap(await log.acquire(branch, "older"));
    unwrap(
      await writer.append([
        {
          type: "thread_started",
          type_version: 1,
          critical: true,
          actor: { kind: "host" },
          data: { ...legacy, config_hash: configHash(legacy) },
        },
      ]),
    );
    await writer.release();
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
    const ran = await run(store, scriptedModel({ responses: [say("Hi.")] }));
    // A real pin, as if the host crashed right after thread_started.
    await rewriteLog(store, ran.id, (lines) => lines.slice(0, 1));
    const handle = unwrap(await openThread(store, ran.id));
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
    await db.transaction((tx) =>
      tx.run(
        'UPDATE events SET line = CAST(replace(CAST(line AS TEXT), \'"type":"turn_completed"\', \'"type":"approval_quorum"\') AS BLOB) WHERE branch_id = ?',
        [thread.branch],
      ),
    );
    expect([
      code(await thread.usage()),
      code(await thread.cost({ tree: true })),
      code(await thread.cacheBreaks()),
      code(await thread.timeline()),
    ]).toEqual(Array(4).fill("unsupported_critical_event"));
  });

  test("a branch gone after open is log_corrupt, the declared error", async () => {
    const store = sqlite(":memory:");
    const thread = await run(store, priced([say("Hi.")]));
    const { db } = await storeConnection(store);
    // Another tenant's now: to this store the branch no longer exists.
    // PRAGMA foreign_keys is a no-op inside a transaction: the thread row moves with the branch,
    // checked at commit.
    await db.transaction(async (tx) => {
      await tx.run("PRAGMA defer_foreign_keys = ON", []);
      await tx.run(
        "UPDATE branches SET tenant_id = 'elsewhere' WHERE branch_id = ?",
        [thread.branch],
      );
      await tx.run(
        "UPDATE threads SET tenant_id = 'elsewhere' WHERE thread_id = ?",
        [thread.id],
      );
    });
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
