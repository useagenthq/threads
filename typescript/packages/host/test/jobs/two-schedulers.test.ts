import { afterEach, describe, expect, test } from "bun:test";
import { LogStore, memoryArtifacts } from "@threads/core";
import {
  type KnownEvent,
  knownEvents,
  type StoreDriver,
  ThreadId,
} from "@threads/core/host";
import { z } from "zod";
import { sqlAll } from "../sql";
import { finish, go, logged, reap, scratch, spawn } from "./drill";
import { drillDriver } from "./stores";
import { rows } from "./worker";

// Job schedule-two-schedulers-one-run: two host processes tick the same
// schedule over the same due occurrences. Each occurrence is claimed once and runs once.

afterEach(reap);

const MINUTE = 60_000;
const FIRST = Date.parse("2026-05-01T09:00:00Z");
const OCCURRENCES = Array.from({ length: 8 }, (_, i) => FIRST + i * MINUTE);

describe("schedule-two-schedulers-one-run", () => {
  test("two schedulers, each occurrence once", async () => {
    const dir = scratch();
    const env = {
      DRILL_GO: "1",
      DRILL_OCCURRENCES: JSON.stringify(OCCURRENCES),
    };
    const a = spawn("schedule", dir, env);
    const b = spawn("schedule", dir, env);
    go(dir);
    expect(await finish(a)).toBe(0);
    expect(await finish(b)).toBe(0);

    const db = (await drillDriver(dir)).db;
    try {
      const claimed = z
        .array(z.strictObject({ occurrence_at: z.int(), state: z.string() }))
        .parse(
          await sqlAll(
            db,
            "SELECT occurrence_at, state FROM schedule_occurrences ORDER BY occurrence_at",
            [],
          ),
        );
      // Each occurrence is claimed exactly once, by one scheduler.
      expect(claimed.map((c) => c.occurrence_at)).toEqual(OCCURRENCES);
      const all = await scheduleLog(db);
      const fired = all.flatMap((e) =>
        e.type === "schedule_fired" ? [e.data.scheduled_for] : [],
      );
      const skipped = all.flatMap((e) =>
        e.type === "schedule_skipped" ? [e.data.reason] : [],
      );
      // One fired an occurrence while the other's run still held the thread: an overlap, run never.
      expect(fired).toEqual(
        claimed.filter((c) => c.state === "fired").map((c) => c.occurrence_at),
      );
      expect(skipped.every((r) => r === "overlap")).toBe(true);
      expect(fired.length + skipped.length).toBe(OCCURRENCES.length);
      expect(fired.length).toBeGreaterThan(0);
      // Every fired occurrence ran exactly once: one input, one model call, across both processes.
      expect(all.filter((e) => e.type === "user_input")).toHaveLength(
        fired.length,
      );
      expect(rows(dir, "model.jsonl")).toHaveLength(fired.length);
      expect(new Set<string>(all.map((e) => e.type))).toEqual(
        new Set([
          "thread_started",
          "schedule_fired",
          "user_input",
          "model_request",
          "model_response",
          "turn_completed",
          ...(skipped.length > 0 ? ["schedule_skipped"] : []),
        ]),
      );
    } finally {
      await db.close();
    }
    expect(logged(dir)).toBe("");
  }, 60_000);
});

/** The schedule's one thread, read back from the log. */
async function scheduleLog(db: StoreDriver): Promise<readonly KnownEvent[]> {
  const [row] = z
    .array(z.strictObject({ thread_id: ThreadId }))
    .parse(
      await sqlAll(
        db,
        "SELECT DISTINCT thread_id FROM schedule_occurrences",
        [],
      ),
    );
  const store = await LogStore.open(db, Date.now, memoryArtifacts(), "local");
  if (!store.ok || row === undefined) throw new Error("no schedule thread");
  const main = await store.value.mainBranch(row.thread_id);
  const read = main.ok ? await store.value.read(main.value) : undefined;
  if (read?.ok !== true) throw new Error("no schedule log");
  return knownEvents(read.value);
}
