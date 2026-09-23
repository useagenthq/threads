import { describe, expect, test } from "bun:test";
import { sqlite } from "@threads/core";
import {
  openStore,
  storeConnection,
  ThreadId,
  tenantStore,
} from "@threads/core/host";
import { z } from "zod";
import { HostContext } from "../src/context";
import { occurrences, parseCron } from "../src/cron";
import { bindSchedules, tick } from "../src/schedules";
import { logOccurrence } from "../src/schedules/decide";
import { pendingRows } from "../src/schedules/rows";
import { eventsOf, mailer, say } from "./kit";

// Cron parsing and DST rules, and the single winner of a pending occurrence. The schedule lifecycle
// is replayed from the shared vector in schedule-threads.test.ts.

const iso = (ms: number): string => new Date(ms).toISOString();

function cron(expr: string) {
  const parsed = parseCron(expr);
  if (!parsed.ok) throw new Error(parsed.error);
  return parsed.value;
}

describe("cron", () => {
  test("fields, lists, ranges and steps; malformed is refused", () => {
    const from = Date.parse("2026-01-01T00:00:00Z");
    expect(
      occurrences(cron("*/30 9-10 * * *"), "UTC", from, from + 86_400_000).map(
        iso,
      ),
    ).toEqual([
      "2026-01-01T09:00:00.000Z",
      "2026-01-01T09:30:00.000Z",
      "2026-01-01T10:00:00.000Z",
      "2026-01-01T10:30:00.000Z",
    ]);
    expect(parseCron("61 * * * *").ok).toBe(false);
    expect(parseCron("* * *").ok).toBe(false);
  });

  test("a nonexistent local time (spring forward) runs at the first valid instant after it", () => {
    const from = Date.parse("2026-03-08T00:00:00Z");
    const at = occurrences(
      cron("30 2 * * *"),
      "America/New_York",
      from,
      from + 86_400_000,
    );
    // 02:30 EST never happens on 2026-03-08; 03:00 EDT is 07:00Z.
    expect(at.map(iso)).toEqual(["2026-03-08T07:00:00.000Z"]);
  });

  test("an ambiguous local time (fall back) runs once, at its first instance", () => {
    const from = Date.parse("2026-11-01T00:00:00Z");
    const at = occurrences(
      cron("30 1 * * *"),
      "America/New_York",
      from,
      from + 86_400_000,
    );
    // 01:30 EDT (05:30Z) and 01:30 EST (06:30Z) are the same wall time: only the first runs.
    expect(at.map(iso)).toEqual(["2026-11-01T05:30:00.000Z"]);
  });
});

const nine = Date.parse("2026-05-01T09:00:00Z");
const DAY = 86_400_000;

async function scheduleThread(store: ReturnType<typeof sqlite>) {
  const { db } = await storeConnection(store);
  const [row] = z
    .array(z.strictObject({ thread_id: ThreadId }))
    .parse(db.all("SELECT thread_id FROM schedule_threads", []));
  const { log } = await openStore(tenantStore(store, "local"));
  if (row === undefined) throw new Error("no schedule thread");
  const main = log.mainBranch(row.thread_id);
  if (!main.ok) throw new Error(main.error.message);
  return { db, log, branch: main.value };
}

function host(store: ReturnType<typeof sqlite>) {
  const ctx = new HostContext(
    store,
    { support: mailer({ responses: [say("ok"), say("ok")] }) },
    {},
  );
  const bound = bindSchedules(ctx, [
    { id: "daily", agent: "support", cron: "0 9 * * *", input: "Report." },
  ]);
  if (typeof bound === "string") throw new Error(bound);
  return { ctx, bound };
}

describe("scheduler", () => {
  test("two schedulers holding one pending row after a restart: the stale one appends nothing", async () => {
    const store = sqlite(":memory:");
    const a = host(store);
    await tick(a.ctx, a.bound, nine - 60_000, nine + 1_000);
    await a.ctx.idle();
    // An outbound delivery holds the writer when the next occurrence falls due: it stays pending.
    const { db, log, branch } = await scheduleThread(store);
    const outbound = log.acquire(branch, "outbound");
    if (!outbound.ok) throw new Error(outbound.error.message);
    await tick(a.ctx, a.bound, nine - 60_000, nine + DAY + 1_000);
    outbound.value.release();
    // After a restart, two schedulers both select it; one decides it first.
    const [stale] = pendingRows(db, "local");
    if (stale === undefined) throw new Error("no pending row");
    const b = host(store);
    await tick(b.ctx, b.bound, nine + DAY + 60_000, nine + DAY + 60_000);
    await b.ctx.idle();
    const writer = log.acquire(branch, "stale-scheduler");
    if (!writer.ok) throw new Error(writer.error.message);
    const pass = { ctx: b.ctx, db, log, tenant: "local" };
    expect(logOccurrence(pass, writer.value, stale, null)).toBe(false);
    writer.value.release();
    const logged = (await eventsOf(store, "local", branch)).filter(
      (e) => e.type === "schedule_fired" || e.type === "schedule_skipped",
    );
    expect(logged.map((e) => e.type)).toEqual([
      "schedule_fired",
      "schedule_fired",
    ]);
    expect(
      db.all(
        "SELECT state, logged_seq IS NOT NULL AS logged FROM schedule_occurrences ORDER BY occurrence_at",
        [],
      ),
    ).toEqual([
      { state: "fired", logged: 1 },
      { state: "fired", logged: 1 },
    ]);
    await a.ctx.stop();
    await b.ctx.stop();
  });
});
