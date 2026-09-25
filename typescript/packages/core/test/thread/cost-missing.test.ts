import { describe, expect, test } from "bun:test";
import { agent, scriptedModel, sqlite } from "../../src";
import { storeConnection } from "../../src/agent/sqlite";
import { code, unwrap } from "../store/helpers";
import { type Line, obj, rewriteLog } from "./rewrite-log";
import { childIds, ONE, priced, run, say, spawn, tree, usd } from "./usage-kit";

// Thread.cost({ tree: true }) when a spawned child has no log: nothing proves it spent nothing,
// so the total is incomplete (or null when nothing in the tree is priced), and a finished
// child's missing log is log_corrupt (lane 04 spec, As built).

const free = (name: string) =>
  agent({ name, model: scriptedModel({ responses: [say("Free.")] }) });

describe("a spawned child with no log", () => {
  test("an unpriced tree with a missing child log is null: unknown, never zero", async () => {
    const store = sqlite(":memory:");
    const lead = scriptedModel({ responses: [spawn("free"), say("Done.")] });
    const thread = await run(store, lead, [free("free")]);
    const [freeId] = await childIds(thread);
    if (freeId === undefined) throw new Error("no child");
    const unstarted = "0192a000-0000-7000-8000-0000000000eb";
    await rewriteLog(store, thread.id, (lines) => {
      const at = lines.findIndex((l) => l["type"] === "agent_spawned");
      return lines
        .slice(0, at + 1)
        .map((l) =>
          obj(JSON.parse(JSON.stringify(l).replaceAll(freeId, unstarted))),
        );
    });
    expect(unwrap(await tree(thread))).toBeNull();
  });

  test("a spawned child with no log counts nothing, and the total is incomplete", async () => {
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
    // Only the lead's first response (the spawn) was recorded. Nothing proves the child never
    // ran (its log could have been deleted), so the total is not complete.
    expect(unwrap(await tree(thread))).toEqual(usd(ONE, false));
  });

  test("two spawns naming the same missing child are log_corrupt", async () => {
    const store = sqlite(":memory:");
    const one = agent({ name: "one", model: priced([say("One.")]) });
    const two = agent({ name: "two", model: priced([say("Two.")]) });
    const thread = await run(
      store,
      priced([spawn("one"), spawn("two"), say("Done.")]),
      [one, two],
    );
    const ids = await childIds(thread);
    const missing = "0192a000-0000-7000-8000-0000000000ec";
    await rewriteLog(store, thread.id, (lines) => {
      const spawns = lines.flatMap((l, i) =>
        l["type"] === "agent_spawned" ? [i] : [],
      );
      return lines.slice(0, (spawns[1] ?? 0) + 1).map((l) => {
        const text = ids.reduce<string>(
          (s, id) => s.replaceAll(id, missing),
          JSON.stringify(l),
        );
        const line = obj(JSON.parse(text));
        return line["type"] === "agent_finished"
          ? {
              ...line,
              data: {
                ...obj(line["data"]),
                status: "cancelled",
                usage: { input_tokens: null, output_tokens: null },
              },
            }
          : line;
      });
    });
    const total = await tree(thread);
    expect(code(total)).toBe("log_corrupt");
    expect(total.ok ? "" : total.error.message).toContain("appears twice");
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
    await db.transaction((tx) =>
      tx.run(
        "INSERT INTO threads (thread_id, tenant_id) SELECT ?, tenant_id FROM threads WHERE thread_id = ?",
        [elsewhere, kidId ?? ""],
      ),
    );
    await db.transaction((tx) =>
      tx.run("UPDATE branches SET thread_id = ? WHERE thread_id = ?", [
        elsewhere,
        kidId ?? "",
      ]),
    );
    const total = await tree(thread);
    expect(code(total)).toBe("log_corrupt");
    expect(total.ok ? "" : total.error.message).toContain(
      `child ${kidId}: its log is missing`,
    );
  });
});

describe("a cancelled child that was never created", () => {
  // spec/schema/README.md, Subagent cancellation: a child with no thread is recorded cancelled,
  // with unknown usage, and never created.
  async function cancelledBeforeCreated(usage: Line) {
    const store = sqlite(":memory:");
    const kid = agent({ name: "kid", model: priced([say("Kid.")]) });
    const thread = await run(store, priced([spawn("kid"), say("Done.")]), [
      kid,
    ]);
    const [kidId] = await childIds(thread);
    if (kidId === undefined) throw new Error("no child");
    const unstarted = "0192a000-0000-7000-8000-0000000000ed";
    await rewriteLog(store, thread.id, (lines) =>
      lines.map((l) => {
        const line = obj(
          JSON.parse(JSON.stringify(l).replaceAll(kidId, unstarted)),
        );
        return line["type"] === "agent_finished"
          ? {
              ...line,
              data: { ...obj(line["data"]), status: "cancelled", usage },
            }
          : line;
      }),
    );
    return tree(thread);
  }

  test("counts nothing, and the total is incomplete: the record can't prove it never ran", async () => {
    // A started child cancelled with unknown usage writes the same record; if its log were
    // lost, a complete total would hide its spend.
    const total = await cancelledBeforeCreated({
      input_tokens: null,
      output_tokens: null,
    });
    expect(unwrap(total)).toEqual(usd(2 * ONE, false));
  });

  test("with known usage its missing log is log_corrupt", async () => {
    const total = await cancelledBeforeCreated({
      input_tokens: 10,
      output_tokens: 2,
    });
    expect(code(total)).toBe("log_corrupt");
    expect(total.ok ? "" : total.error.message).toContain(
      "its log is missing, though its parent recorded it cancelled",
    );
  });
});
