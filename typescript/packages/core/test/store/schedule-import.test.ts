import { describe, expect, test } from "bun:test";
import type { EventDraft } from "../../src/store";
import { code, fixture, ROOT, rows, started, THREAD, unwrap } from "./helpers";

// Importing a schedule thread rebuilds its indices (spec/schema/README.md, "Portable bundles"):
// the schedule's identity and its decided occurrence rows come from the chain's own schedule
// events, claimed again inside the row transaction so a scheduler can't lose or split a schedule.

const SCHEDULE = "daily_digest";
const AT = 1_790_000_600_000;
const OCCURRENCE = `${SCHEDULE}@2026-09-26T00:03:20.000Z`;
const OTHER = "0192a000-0000-7000-8000-0000000000ff";

const fired = (occurrenceId: string, at: number): EventDraft => ({
  type: "schedule_fired",
  type_version: 1,
  critical: true,
  actor: { kind: "scheduler" },
  data: {
    schedule_id: SCHEDULE,
    occurrence_id: occurrenceId,
    scheduled_for: at,
    timezone: "UTC",
  },
});

/** A schedule thread's export: thread_started, then one fired occurrence. */
async function exported(): Promise<Uint8Array> {
  const f = await fixture();
  unwrap(await f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(await f.store.acquire(ROOT, "setup"));
  unwrap(await writer.append([started, fired(OCCURRENCE, AT)]));
  await writer.release();
  return unwrap(await f.store.exportBranch(ROOT));
}

describe("importing a schedule thread", () => {
  test("rebuilds the schedule's identity and its decided occurrence row", async () => {
    const bytes = await exported();
    const into = await fixture();
    unwrap(await into.store.importLog(bytes));
    expect(
      await rows(
        into.db,
        "SELECT schedule_id, thread_id, current FROM schedule_threads",
      ),
    ).toEqual([{ schedule_id: SCHEDULE, thread_id: THREAD, current: 1 }]);
    expect(
      await rows(
        into.db,
        `SELECT schedule_id, occurrence_at, state, reason, thread_id, logged_seq
          FROM schedule_occurrences`,
      ),
    ).toEqual([
      {
        schedule_id: SCHEDULE,
        occurrence_at: AT,
        state: "fired",
        reason: null,
        thread_id: THREAD,
        logged_seq: 2,
      },
    ]);
  });

  test("a second import of the same bundle changes nothing: the keys are already this thread's", async () => {
    const bytes = await exported();
    const into = await fixture();
    unwrap(await into.store.importLog(bytes));
    unwrap(await into.store.importLog(bytes));
    expect((await rows(into.db, "SELECT thread_id FROM schedule_threads")).length).toBe(1);
    expect(
      (await rows(into.db, "SELECT occurrence_at FROM schedule_occurrences")).length,
    ).toBe(1);
  });

  test("a schedule already running on another thread here is schedule_conflict, and nothing is stored", async () => {
    const bytes = await exported();
    const into = await fixture();
    // A scheduler got there first with its own thread for the same schedule id.
    await into.db.transaction((tx) =>
      tx.run(
        `INSERT INTO schedule_threads (tenant_id, schedule_id, thread_id, current, created_at)
          VALUES (?, ?, ?, 1, ?)`,
        [into.store.tenant, SCHEDULE, OTHER, into.clock.now],
      ),
    );
    const refused = await into.store.importLog(bytes);
    expect(code(refused)).toBe("schedule_conflict");
    expect(await rows(into.db, "SELECT branch_id FROM branches")).toEqual([]);
    expect(await rows(into.db, "SELECT occurrence_at FROM schedule_occurrences")).toEqual([]);
  });

  test("an occurrence another thread already decided is schedule_conflict", async () => {
    const bytes = await exported();
    const into = await fixture();
    await into.db.transaction((tx) =>
      tx.run(
        `INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at, state, reason,
          thread_id, claimed_at) VALUES (?, ?, ?, 'skipped', 'overlap', ?, ?)`,
        [into.store.tenant, SCHEDULE, AT, THREAD, into.clock.now],
      ),
    );
    expect(code(await into.store.importLog(bytes))).toBe("schedule_conflict");
    expect(await rows(into.db, "SELECT branch_id FROM branches")).toEqual([]);
  });
});
