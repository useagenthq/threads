import { describe, expect, test } from "bun:test";
import { sqlite } from "@threads/core";
import { storeConnection } from "@threads/core/host";
import { z } from "zod";
import { HostContext } from "../src/context";
import { occurrences, parseCron } from "../src/cron";
import { bindSchedules, type Schedule, tick } from "../src/schedules";
import { eventsOf, mailer, say } from "./kit";

// Schedules: occurrence identity, DST rules, missed and overlapping occurrences,
// and one run when two schedulers see the same due occurrence.

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

const Row = z.strictObject({
  occurrence_at: z.int(),
  state: z.string(),
  reason: z.string().nullable(),
  thread_id: z.string().nullable(),
});

function setup(
  schedule: Omit<Schedule, "agent"> = {
    id: "daily",
    cron: "0 9 * * *",
    input: "Report.",
  },
) {
  const store = sqlite(":memory:");
  const ctx = new HostContext(
    store,
    {
      support: mailer({
        responses: [say("ok"), say("ok"), say("ok"), say("ok")],
      }),
    },
    {},
  );
  const bound = bindSchedules(ctx, [{ ...schedule, agent: "support" }]);
  if (typeof bound === "string") throw new Error(bound);
  return { store, ctx, bound };
}

async function rows(store: ReturnType<typeof sqlite>) {
  const { db } = await storeConnection(store);
  return z
    .array(Row)
    .parse(
      db.all(
        "SELECT occurrence_at, state, reason, thread_id FROM schedule_occurrences ORDER BY occurrence_at",
        [],
      ),
    );
}

async function scheduleEvents(store: ReturnType<typeof sqlite>) {
  const [row] = await rows(store);
  if (row?.thread_id === null || row === undefined) return [];
  const { db } = await storeConnection(store);
  const [branch] = z
    .array(z.strictObject({ branch_id: z.string() }))
    .parse(
      db.all("SELECT branch_id FROM branches WHERE thread_id = ?", [
        row.thread_id,
      ]),
    );
  return eventsOf(store, "local", branch?.branch_id ?? "");
}

const DAY = 86_400_000;
const nine = Date.parse("2026-05-01T09:00:00Z");

describe("scheduler", () => {
  test("ready triggers nothing; the due occurrence fires once with schedule_fired then its input", async () => {
    const { store, ctx, bound } = setup();
    const startedAt = nine - 60_000;
    await tick(ctx, bound, startedAt, startedAt);
    expect(await rows(store)).toEqual([]);
    await tick(ctx, bound, startedAt, nine + 1_000);
    await tick(ctx, bound, startedAt, nine + 2_000);
    await ctx.stop();
    expect((await rows(store)).map((r) => r.state)).toEqual(["fired"]);
    const types = (await scheduleEvents(store)).map((e) => e.type);
    expect(types.slice(0, 3)).toEqual([
      "thread_started",
      "schedule_fired",
      "user_input",
    ]);
    expect(types).toContain("turn_completed");
  });

  test("two schedulers seeing the same due occurrence start one run", async () => {
    const { store, ctx, bound } = setup();
    const startedAt = nine - 60_000;
    await Promise.all([
      tick(ctx, bound, startedAt, nine + 1_000),
      tick(ctx, bound, startedAt, nine + 1_000),
    ]);
    await ctx.stop();
    expect(await rows(store)).toHaveLength(1);
    const fired = (await scheduleEvents(store)).filter(
      (e) => e.type === "schedule_fired",
    );
    expect(fired).toHaveLength(1);
  });

  test("occurrences missed while the host was down are recorded as missed and run nothing", async () => {
    const { store, ctx, bound } = setup();
    await tick(ctx, bound, nine - 60_000, nine + 1_000);
    await ctx.stop();
    const again = new HostContext(
      store,
      { support: mailer({ responses: [] }) },
      {},
    );
    const rebound = bindSchedules(again, [
      { id: "daily", cron: "0 9 * * *", input: "Report.", agent: "support" },
    ]);
    if (typeof rebound === "string") throw new Error(rebound);
    const restart = nine + 3 * DAY + 3_600_000;
    await tick(again, rebound, restart, restart);
    await again.stop();
    const recorded = await rows(store);
    expect(recorded.map((r) => [r.state, r.reason])).toEqual([
      ["fired", null],
      ["skipped", "missed"],
      ["skipped", "missed"],
      ["skipped", "missed"],
    ]);
    const skipped = (await scheduleEvents(store)).filter(
      (e) => e.type === "schedule_skipped",
    );
    expect(skipped).toHaveLength(3);
  });
});
