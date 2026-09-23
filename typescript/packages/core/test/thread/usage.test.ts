import { describe, expect, test } from "bun:test";
import { agent, openThread, scriptedModel, sqlite } from "../../src";
import { openStore, storeConnection } from "../../src/agent/sqlite";
import { BranchId, ThreadId } from "../../src/log";
import { markTestKit } from "../../src/model/guard";
import type { Model } from "../../src/model/protocol";
import { cost, knownEvents, mergeCost, reduce } from "../../src/reduce";
import type { Thread } from "../../src/thread";
import { code, started, unwrap } from "../store/helpers";

// Thread.usage(), cost() and cacheBreaks() (spec/api.json) through openThread over real runs:
// each is the projection of one verified read, and a corrupt log is log_corrupt, never zero.

const PRICE = { input: 3000, output: 15_000 };
// One scripted response of 10 input and 2 output tokens at PRICE.
const ONE = 10 * 3000 + 2 * 15_000;
const usage = { input_tokens: 10, output_tokens: 2 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
const spawn = (name: string) => ({
  content: [
    {
      type: "tool_use",
      call_id: `s-${name}`,
      name: "spawn_agent",
      input: { agent: name, prompt: "Do it." },
    },
  ],
  stop_reason: "tool_use",
  usage,
});

type Store = ReturnType<typeof sqlite>;

/** A scripted model that declares a price; still test kit, so the request guard allows it. */
function priced(responses: readonly unknown[]): Model {
  const base = scriptedModel({ responses });
  const model: Model = {
    ...base,
    info: { ...base.info, limits: { ...base.info.limits, price: PRICE } },
  };
  markTestKit(model);
  return model;
}

async function run(
  store: Store,
  model: Model,
  subagents: Parameters<typeof agent>[0]["subagents"] = [],
): Promise<Thread> {
  const result = await agent({ name: "lead", model, subagents }).run("Go.", {
    store,
  });
  return unwrap(await openThread(store, result.thread.id));
}

/** Rewrites one stored line of `thread`'s main branch so its chain no longer verifies. */
async function corrupt(
  store: Store,
  thread: ThreadId,
  prompt: string,
): Promise<void> {
  const { db } = await storeConnection(store);
  const { log } = await openStore(store);
  const branch = unwrap(log.mainBranch(thread));
  db.run(
    "UPDATE events SET line = CAST(replace(CAST(line AS TEXT), ?, 'Edited.') AS BLOB) WHERE branch_id = ? AND seq = 2",
    [prompt, branch],
  );
}

async function childIds(thread: Thread): Promise<readonly ThreadId[]> {
  return (await thread.children()).map((c) =>
    ThreadId.parse(c.child_thread_id),
  );
}

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

  test("an unpriced root is null even over priced children", async () => {
    const store = sqlite(":memory:");
    const child = agent({ name: "kid", model: priced([say("Kid.")]) });
    const lead = scriptedModel({ responses: [spawn("kid"), say("Done.")] });
    const thread = await run(store, lead, [child]);
    expect(unwrap(await thread.cost({ tree: true }))).toBeNull();
  });

  test("a part in another currency adds nothing and makes the total incomplete", () => {
    const usd = {
      currency: "USD",
      known_nanos: 5,
      upper_bound_nanos: 7,
      complete: true,
      bounded: true,
    };
    expect(mergeCost(usd, { ...usd, currency: "EUR" })).toEqual({
      ...usd,
      complete: false,
      bounded: false,
    });
    expect(mergeCost(usd, { ...usd, complete: false })).toEqual({
      ...usd,
      known_nanos: 10,
      upper_bound_nanos: 14,
      complete: false,
    });
  });

  test("a priced thread pinned before currency reads, costs null and refuses to continue", async () => {
    // What an older agent() stored: the same pin without policy.currency, so another hash.
    const fresh = await run(sqlite(":memory:"), priced([say("Hi.")]));
    const { log: freshLog } = await openStore(fresh.store);
    const [first] = knownEvents(unwrap(freshLog.read(fresh.branch)));
    if (first?.type !== "thread_started") throw new Error("no thread_started");
    const { currency: _currency, ...policy } = first.data.policy ?? {};
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
          data: { ...first.data, policy, config_hash: "0".repeat(64) },
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

  test("tree adds the child and grandchild from their own logs", async () => {
    const store = sqlite(":memory:");
    const leaf = agent({ name: "leaf", model: priced([say("Leaf.")]) });
    const mid = agent({
      name: "mid",
      model: priced([spawn("leaf"), say("Mid.")]),
      subagents: [leaf],
    });
    const thread = await run(store, priced([spawn("mid"), say("Done.")]), [
      mid,
    ]);
    expect(unwrap(await thread.cost())?.known_nanos).toBe(2 * ONE);
    // Lead 2 responses, mid 2, leaf 1.
    expect(unwrap(await thread.cost({ tree: true }))).toEqual({
      currency: "USD",
      known_nanos: 5 * ONE,
      upper_bound_nanos: 5 * ONE,
      complete: true,
      bounded: true,
    });
  });

  test("an unpriced child adds nothing and makes the total incomplete and unbounded", async () => {
    const store = sqlite(":memory:");
    const child = agent({
      name: "free",
      model: scriptedModel({ responses: [say("Free.")] }),
    });
    const thread = await run(store, priced([spawn("free"), say("Done.")]), [
      child,
    ]);
    expect(unwrap(await thread.cost({ tree: true }))).toEqual({
      currency: "USD",
      known_nanos: 2 * ONE,
      upper_bound_nanos: 2 * ONE,
      complete: false,
      bounded: false,
    });
  });

  test("a spawned child with no thread was never started and adds nothing", async () => {
    const store = sqlite(":memory:");
    const child = agent({ name: "kid", model: priced([say("Kid.")]) });
    const thread = await run(store, priced([spawn("kid"), say("Done.")]), [
      child,
    ]);
    const [kid] = await childIds(thread);
    // As if the parent crashed between agent_spawned and the child's first line.
    const { db } = await storeConnection(store);
    const elsewhere = "0192a000-0000-7000-8000-0000000000ee";
    db.run(
      "INSERT INTO threads (thread_id, tenant_id) SELECT ?, tenant_id FROM threads WHERE thread_id = ?",
      [elsewhere, kid ?? ""],
    );
    db.run("UPDATE branches SET thread_id = ? WHERE thread_id = ?", [
      elsewhere,
      kid ?? "",
    ]);
    expect(unwrap(await thread.cost({ tree: true }))?.known_nanos).toBe(
      2 * ONE,
    );
  });

  test("a corrupt child fails the tree with log_corrupt, naming the child", async () => {
    const store = sqlite(":memory:");
    const child = agent({ name: "kid", model: priced([say("Kid.")]) });
    const thread = await run(store, priced([spawn("kid"), say("Done.")]), [
      child,
    ]);
    const [kid] = await childIds(thread);
    if (kid === undefined) throw new Error("no child");
    await corrupt(store, kid, "Do it.");
    const tree = await thread.cost({ tree: true });
    expect(code(tree)).toBe("log_corrupt");
    expect(tree.ok ? "" : tree.error.message).toContain(kid);
    expect(unwrap(await thread.cost())?.known_nanos).toBe(2 * ONE);
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
