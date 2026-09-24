import { describe, expect, test } from "bun:test";
import { agent, extension, scriptedModel, sqlite } from "@threads/core";
import {
  openStore,
  storeConnection,
  ThreadId,
  tenantStore,
} from "@threads/core/host";
import { z } from "zod";
import { HostContext } from "../src/context";
import { bindSchedules, tick } from "../src/schedules";
import { say } from "./kit";

// A failing schedule, agent or thread is reported and never stops the others: every other
// schedule is reserved and decided, and recovery always runs.

const nine = Date.parse("2026-05-01T09:00:00Z");
const DAY = 86_400_000;

function broken(name: string) {
  return agent({
    name,
    model: scriptedModel({ responses: [] }),
    extensions: [
      extension({
        name: "boot",
        setup: async () => {
          throw new Error("no creds");
        },
      }),
    ],
  });
}

function healthy(name: string) {
  return agent({
    name,
    model: scriptedModel({ responses: [say("ok"), say("ok"), say("ok")] }),
  });
}

async function occurrences(store: ReturnType<typeof sqlite>) {
  const { db } = await storeConnection(store);
  return db.all(
    "SELECT schedule_id, state FROM schedule_occurrences ORDER BY schedule_id, occurrence_at",
    [],
  );
}

describe("schedule failures", () => {
  test("a broken agent's schedule is reported, and a healthy one fires on time", async () => {
    const store = sqlite(":memory:");
    const ctx = new HostContext(
      store,
      { bad: broken("bad"), good: healthy("good") },
      {},
    );
    const bound = bindSchedules(ctx, [
      { id: "broken", agent: "bad", cron: "0 9 * * *", input: "Hi." },
      { id: "daily", agent: "good", cron: "0 9 * * *", input: "Hi." },
    ]);
    if (typeof bound === "string") throw new Error(bound);
    await tick(ctx, bound, nine - 60_000, nine + 1_000);
    await ctx.idle();
    expect(await occurrences(store)).toEqual([
      { schedule_id: "daily", state: "fired" },
    ]);
    await ctx.stop();
  });

  test("a corrupt pending row is reported, and recovery still resumes an open turn", async () => {
    const store = sqlite(":memory:");
    const ctx = new HostContext(store, { good: healthy("good") }, {});
    const bound = bindSchedules(ctx, [
      { id: "daily", agent: "good", cron: "0 9 * * *", input: "Hi." },
    ]);
    if (typeof bound === "string") throw new Error(bound);
    await tick(ctx, bound, nine - 60_000, nine + 1_000);
    await ctx.idle();
    const { db } = await storeConnection(store);
    const [row] = z
      .array(z.strictObject({ thread_id: ThreadId }))
      .parse(db.all("SELECT thread_id FROM schedule_threads", []));
    if (row === undefined) throw new Error("no schedule thread");
    const { log } = await openStore(tenantStore(store, "local"));
    const main = log.mainBranch(row.thread_id);
    if (!main.ok) throw new Error(main.error.message);
    // A run cut short: an input durable, its turn open, no run going.
    const writer = log.acquire(main.value, "crashed");
    if (!writer.ok) throw new Error(writer.error.message);
    writer.value.append([
      {
        type: "user_input",
        type_version: 1,
        critical: true,
        actor: {
          kind: "user",
          principal: { issuer: "api", tenant: "local", subject: "op" },
        },
        data: { source: "api", text: "Busy." },
      },
    ]);
    writer.value.release();
    db.run(
      `INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at, state, thread_id,
        claimed_at, agent, input_json, timezone)
        VALUES ('local', 'other', 1, 'pending', 'not-a-thread-id', 1, 'good', '"Hi."', 'UTC')`,
      [],
    );
    await tick(ctx, bound, nine - 60_000, nine + DAY - 60_000);
    await ctx.idle();
    const read = log.read(main.value);
    expect(read.ok && read.value.fold.turnOpen).toBe(false);
    await ctx.stop();
  });
});
