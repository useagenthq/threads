import { describe, expect, test } from "bun:test";
import { agent, openThread, scriptedModel, sqlite } from "../../src";
import { storeConnection } from "../../src/agent/sqlite";
import { Cost, type ThreadId } from "../../src/log";
import type { Thread } from "../../src/thread";
import { code, unwrap } from "../store/helpers";
import { editStarted, obj, rewriteLog } from "./rewrite-log";
import {
  childIds,
  corrupt,
  ONE,
  priced,
  run,
  type Store,
  say,
  spawn,
  unpricedMiddle,
} from "./usage-kit";

// Thread.cost({ tree: true }) over real child logs: the tree merge of spec/api.json Thread.cost,
// and the tree's integrity (each child names the spawn that started it; no thread twice).

/** A USD total whose upper bound is its known cost (every attempt here settles). */
const usd = (known: number, exact: boolean): Cost => ({
  currency: "USD",
  known_nanos: known,
  upper_bound_nanos: known,
  complete: exact,
  bounded: exact,
});

const tree = async (thread: Thread) => thread.cost({ tree: true });

async function handle(store: Store, id: ThreadId | undefined): Promise<Thread> {
  if (id === undefined) throw new Error("no such child");
  return unwrap(await openThread(store, id));
}

/** The first agent_spawned of `thread`: what its child's thread_started must name. */
async function firstSpawn(thread: Thread) {
  const entry = unwrap(await thread.timeline()).entries.find(
    (e) => e.event.type === "agent_spawned",
  );
  if (entry === undefined) throw new Error("no agent_spawned");
  const { thread_id, branch_id, event_id } = entry.event;
  return { thread_id, branch_id, event_id, relation: "subagent" };
}

const free = (name: string, responses: readonly unknown[] = [say("Free.")]) =>
  agent({ name, model: scriptedModel({ responses }) });

describe("the tree merge", () => {
  test("adds the child and grandchild from their own logs", async () => {
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
    expect(unwrap(await thread.cost())).toEqual(usd(2 * ONE, true));
    // Lead 2 responses, mid 2, leaf 1.
    expect(unwrap(await tree(thread))).toEqual(usd(5 * ONE, true));
  });

  test("an unpriced root counts in its first priced descendant's currency, incomplete", async () => {
    const store = sqlite(":memory:");
    const kid = agent({ name: "kid", model: priced([say("Kid.")]) });
    const lead = scriptedModel({ responses: [spawn("kid"), say("Done.")] });
    const thread = await run(store, lead, [kid]);
    expect(unwrap(await thread.cost())).toBeNull();
    expect(unwrap(await tree(thread))).toEqual(usd(ONE, false));
  });

  test("an all-unpriced tree is null", async () => {
    const store = sqlite(":memory:");
    const lead = scriptedModel({ responses: [spawn("free"), say("Done.")] });
    const thread = await run(store, lead, [free("free")]);
    expect(unwrap(await tree(thread))).toBeNull();
  });

  test("mixed siblings: an unpriced child that ran makes the total incomplete", async () => {
    const store = sqlite(":memory:");
    const kid = agent({ name: "kid", model: priced([say("Kid.")]) });
    const lead = priced([spawn("kid"), spawn("free"), say("Done.")]);
    const thread = await run(store, lead, [kid, free("free")]);
    expect(unwrap(await tree(thread))).toEqual(usd(4 * ONE, false));
  });

  test("several unpriced levels don't stop the walk", async () => {
    const store = sqlite(":memory:");
    const leaf = agent({ name: "leaf", model: priced([say("Leaf.")]) });
    const low = agent({
      name: "low",
      model: scriptedModel({ responses: [spawn("leaf"), say("Low.")] }),
      subagents: [leaf],
    });
    const high = agent({
      name: "high",
      model: scriptedModel({ responses: [spawn("low"), say("High.")] }),
      subagents: [low],
    });
    const thread = await run(store, priced([spawn("high"), say("Done.")]), [
      high,
    ]);
    // Lead 2 responses and leaf 1; the unpriced levels add nothing but their lack of a price.
    expect(unwrap(await tree(thread))).toEqual(usd(3 * ONE, false));
  });

  test("a child in another currency adds nothing and makes the total incomplete", async () => {
    const store = sqlite(":memory:");
    const eur = agent({ name: "eur", model: priced([say("Euro.")]) });
    const kid = agent({ name: "kid", model: priced([say("Kid.")]) });
    const lead = priced([spawn("eur"), spawn("kid"), say("Done.")]);
    const thread = await run(store, lead, [eur, kid]);
    const [eurId] = await childIds(thread);
    if (eurId === undefined) throw new Error("no child");
    await rewriteLog(
      store,
      eurId,
      editStarted((d) => ({
        ...d,
        policy: { ...obj(d["policy"]), currency: "EUR" },
      })),
    );
    expect(unwrap(await (await handle(store, eurId)).cost())?.currency).toBe(
      "EUR",
    );
    expect(unwrap(await tree(thread))).toEqual(usd(4 * ONE, false));
  });

  test("a started child that made no model request changes nothing", async () => {
    const store = sqlite(":memory:");
    const thread = await run(store, priced([spawn("free"), say("Done.")]), [
      free("free"),
    ]);
    expect(unwrap(await tree(thread))).toEqual(usd(2 * ONE, false));
    const [freeId] = await childIds(thread);
    if (freeId === undefined) throw new Error("no child");
    // As if it crashed after its input, before its first request: thread_started, user_input.
    await rewriteLog(store, freeId, (lines) => lines.slice(0, 2));
    expect(unwrap(await tree(thread))).toEqual(usd(2 * ONE, true));
  });

  test("a spawned child that never started changes nothing", async () => {
    const store = sqlite(":memory:");
    const kid = agent({ name: "kid", model: priced([say("Kid.")]) });
    const thread = await run(store, priced([spawn("kid"), say("Done.")]), [
      kid,
    ]);
    const [kidId] = await childIds(thread);
    if (kidId === undefined) throw new Error("no child");
    // As if the host crashed right after agent_spawned: the spawn names a child with no thread,
    // and nothing after it (no agent_finished) was written.
    const unstarted = "0192a000-0000-7000-8000-0000000000ee";
    await rewriteLog(store, thread.id, (lines) => {
      const at = lines.findIndex((l) => l["type"] === "agent_spawned");
      return lines
        .slice(0, at + 1)
        .map((l) =>
          obj(JSON.parse(JSON.stringify(l).replaceAll(kidId, unstarted))),
        );
    });
    // Only the lead's first response (the spawn) was recorded.
    expect(unwrap(await tree(thread))).toEqual(usd(ONE, true));
  });
});

describe("a tree that can't be read fails the call, naming the path", () => {
  test("a corrupt child is log_corrupt", async () => {
    const store = sqlite(":memory:");
    const kid = agent({ name: "kid", model: priced([say("Kid.")]) });
    const thread = await run(store, priced([spawn("kid"), say("Done.")]), [
      kid,
    ]);
    const [kidId] = await childIds(thread);
    if (kidId === undefined) throw new Error("no child");
    await corrupt(store, kidId, "Do it.");
    const total = await tree(thread);
    expect(code(total)).toBe("log_corrupt");
    expect(total.ok ? "" : total.error.message).toContain(`child ${kidId}:`);
    expect(unwrap(await thread.cost())).toEqual(usd(2 * ONE, true));
  });

  test("a finished child whose log is missing is log_corrupt, never a partial sum", async () => {
    const store = sqlite(":memory:");
    const kid = agent({ name: "kid", model: priced([say("Kid.")]) });
    const thread = await run(store, priced([spawn("kid"), say("Done.")]), [
      kid,
    ]);
    const [kidId] = await childIds(thread);
    // The parent's agent_finished proves the child ran; its log is gone from this store.
    const { db } = await storeConnection(store);
    const elsewhere = "0192a000-0000-7000-8000-0000000000ef";
    db.run(
      "INSERT INTO threads (thread_id, tenant_id) SELECT ?, tenant_id FROM threads WHERE thread_id = ?",
      [elsewhere, kidId ?? ""],
    );
    db.run("UPDATE branches SET thread_id = ? WHERE thread_id = ?", [
      elsewhere,
      kidId ?? "",
    ]);
    const total = await tree(thread);
    expect(code(total)).toBe("log_corrupt");
    expect(total.ok ? "" : total.error.message).toContain(
      `child ${kidId}: its log is missing`,
    );
  });

  test("a corrupt grandchild under an unpriced child is log_corrupt", async () => {
    const store = sqlite(":memory:");
    const thread = await unpricedMiddle(store);
    const [mid] = await childIds(thread);
    const [leaf] = await childIds(await handle(store, mid));
    if (leaf === undefined) throw new Error("no grandchild");
    await corrupt(store, leaf, "Do it.");
    const total = await tree(thread);
    expect(code(total)).toBe("log_corrupt");
    expect(total.ok ? "" : total.error.message).toContain(
      `child ${mid}: child ${leaf}:`,
    );
  });

  test("a descendant in a newer format is unsupported_format", async () => {
    const store = sqlite(":memory:");
    const kid = agent({ name: "kid", model: priced([say("Kid.")]) });
    const thread = await run(store, priced([spawn("kid"), say("Done.")]), [
      kid,
    ]);
    const [kidId] = await childIds(thread);
    const { db } = await storeConnection(store);
    db.run(
      `UPDATE branches SET header_line = CAST(replace(CAST(header_line AS TEXT), '"format_version":1', '"format_version":2') AS BLOB) WHERE thread_id = ?`,
      [kidId ?? ""],
    );
    const total = await tree(thread);
    expect(code(total)).toBe("unsupported_format");
    expect(total.ok ? "" : total.error.message).toContain(`child ${kidId}:`);
  });

  test("a child whose thread_started names another parent is log_corrupt", async () => {
    const store = sqlite(":memory:");
    const kid = agent({ name: "kid", model: priced([say("Kid.")]) });
    const one = await run(store, priced([spawn("kid"), say("Done.")]), [kid]);
    const other = agent({ name: "kid", model: priced([say("Kid.")]) });
    const two = await run(store, priced([spawn("kid"), say("Done.")]), [other]);
    const [kidId] = await childIds(one);
    if (kidId === undefined) throw new Error("no child");
    // A cross-linked import: one's child claims two's spawn.
    const claimed = await firstSpawn(two);
    await rewriteLog(
      store,
      kidId,
      editStarted((d) => ({ ...d, parent: claimed })),
    );
    const total = await tree(one);
    expect(code(total)).toBe("log_corrupt");
    expect(total.ok ? "" : total.error.message).toContain(
      "doesn't name the agent_spawned",
    );
  });

  test("a cycle is log_corrupt, never an endless walk", async () => {
    const store = sqlite(":memory:");
    const leaf = agent({ name: "leaf", model: priced([say("Leaf.")]) });
    const mid = agent({
      name: "mid",
      model: priced([spawn("leaf"), say("Mid.")]),
      subagents: [leaf],
    });
    const lead = await run(store, priced([spawn("mid"), say("Done.")]), [mid]);
    const [midId] = await childIds(lead);
    const middle = await handle(store, midId);
    const [leafId] = await childIds(middle);
    if (leafId === undefined) throw new Error("no grandchild");
    // mid's log now spawns lead, and lead names that spawn as its parent: lead → mid → lead.
    await rewriteLog(store, middle.id, (lines) =>
      lines.map((l) =>
        obj(JSON.parse(JSON.stringify(l).replaceAll(leafId, lead.id))),
      ),
    );
    const spawnOfLead = await firstSpawn(middle);
    await rewriteLog(
      store,
      lead.id,
      editStarted((d) => ({ ...d, parent: spawnOfLead })),
    );
    const total = await tree(lead);
    expect(code(total)).toBe("log_corrupt");
    expect(total.ok ? "" : total.error.message).toContain(
      `thread ${lead.id} appears twice`,
    );
  });
});

describe("Cost", () => {
  test("rejects a lowercase currency and a fractional amount", () => {
    expect(Cost.safeParse(usd(ONE, true)).success).toBe(true);
    expect(Cost.safeParse({ ...usd(ONE, true), currency: "usd" }).success).toBe(
      false,
    );
    expect(
      Cost.safeParse({ ...usd(ONE, true), known_nanos: 1.5 }).success,
    ).toBe(false);
  });
});
