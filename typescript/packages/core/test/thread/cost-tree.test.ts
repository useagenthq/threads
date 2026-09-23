import { describe, expect, test } from "bun:test";
import { agent, openThread, scriptedModel, sqlite } from "../../src";
import { storeConnection } from "../../src/agent/sqlite";
import { mergeCost } from "../../src/reduce";
import { code, unwrap } from "../store/helpers";
import {
  childIds,
  corrupt,
  ONE,
  priced,
  run,
  say,
  spawn,
  unpricedMiddle,
} from "./usage-kit";

// Thread.cost({ tree: true }): every descendant at any depth, each read from its own log.

describe("Thread.cost({ tree: true })", () => {
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

  test("an unpriced child doesn't stop the walk: its priced child still counts", async () => {
    const store = sqlite(":memory:");
    const thread = await unpricedMiddle(store);
    // Lead 2 responses and leaf 1; mid adds nothing but its lack of a price.
    expect(unwrap(await thread.cost({ tree: true }))).toEqual({
      currency: "USD",
      known_nanos: 3 * ONE,
      upper_bound_nanos: 3 * ONE,
      complete: false,
      bounded: false,
    });
  });

  test("a corrupt grandchild under an unpriced child fails the tree, naming the path", async () => {
    const store = sqlite(":memory:");
    const thread = await unpricedMiddle(store);
    const [mid] = await childIds(thread);
    if (mid === undefined) throw new Error("no child");
    const [leaf] = await childIds(unwrap(await openThread(store, mid)));
    if (leaf === undefined) throw new Error("no grandchild");
    await corrupt(store, leaf, "Do it.");
    const tree = await thread.cost({ tree: true });
    expect(code(tree)).toBe("log_corrupt");
    expect(tree.ok ? "" : tree.error.message).toContain(
      `child ${mid}: child ${leaf}:`,
    );
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
