import { describe, expect } from "bun:test";
import { StoreError } from "@threads/core/store-driver";
import { ok } from "../../core/src/result";
import { LogStore, memoryArtifacts } from "../../core/src/store";
import { openBunSqlite } from "../../core/src/store/bun-sqlite";
import { pgTest } from "./kit";
import {
  pgFixture,
  ROOT,
  started,
  T0,
  THREAD,
  turnCompleted,
  unwrap,
  userInput,
} from "./store-kit";

// Machines sharing one database: one lease holder, fencing that doesn't trust clocks, budgets
// that never overspend, an outage mid-append, and the same bytes on both engines.

describe("two machines on one branch", () => {
  pgTest("racing acquires: one holder, the other branch_busy", async () => {
    const f = await pgFixture();
    try {
      unwrap(await f.store.createBranch(THREAD, ROOT));
      const other = await f.peer();
      const [a, b] = await Promise.all([
        f.store.acquire(ROOT, "a"),
        other.store.acquire(ROOT, "b"),
      ]);
      expect([a.ok, b.ok].toSorted()).toEqual([false, true]);
      const lost = a.ok ? b : a;
      expect(lost.ok ? "ok" : lost.error.code).toBe("branch_busy");
    } finally {
      await f.close();
    }
  });

  pgTest(
    "a takeover by a machine whose clock runs 60 s ahead fences the old holder",
    async () => {
      const f = await pgFixture();
      try {
        unwrap(await f.store.createBranch(THREAD, ROOT));
        const a = unwrap(await f.store.acquire(ROOT, "a"));
        unwrap(await a.append([started]));
        const ahead = { now: T0 + 60_000 };
        const db = (await f.peer()).db;
        const b = unwrap(
          await LogStore.open(db, () => ahead.now, memoryArtifacts()),
        );
        const taken = unwrap(await b.acquire(ROOT, "b"));
        expect(taken.lease.epoch).toBe(a.lease.epoch + 1);
        const late = await a.append([userInput("late")]);
        expect(late.ok ? "ok" : late.error.code).toBe("stale_epoch");
        const fenced = await a.fence();
        expect(fenced.ok ? "ok" : fenced.error.code).toBe("writer_poisoned");
        unwrap(await taken.append([userInput("b goes on")]));
      } finally {
        await f.close();
      }
    },
  );

  pgTest(
    "two machines reserving against one budget never overspend",
    async () => {
      const f = await pgFixture();
      try {
        const other = await f.peer();
        const claim = (i: number) => ({
          budgetId: "thread:t",
          limit: "max_model_requests" as const,
          max: 5,
          amount: 1,
          key: `b:${i}`,
        });
        const tries = Array.from({ length: 12 }, (_, i) => {
          const { key, ...c } = claim(i);
          const ledger = (i % 2 === 0 ? f.store : other.store).budgets;
          return ledger.reserve(key, [c]);
        });
        const refused = await Promise.all(
          tries.map((t) => t.catch((error: unknown) => error)),
        );
        const granted = refused.filter((r) => r === undefined).length;
        expect(granted).toBeLessThanOrEqual(5);
        expect(
          await f.store.budgets.spent("thread:t", "max_model_requests"),
        ).toBe(granted);
      } finally {
        await f.close();
      }
    },
  );

  pgTest(
    "a backend terminated mid-append: an outage, nothing written, the next append goes",
    async () => {
      const f = await pgFixture();
      try {
        unwrap(await f.store.createBranch(THREAD, ROOT));
        const w = unwrap(await f.store.acquire(ROOT, "w"));
        unwrap(await w.append([started]));
        const killed = w.append([userInput("a")], async (_added, tx) => {
          await tx.all("SELECT pg_terminate_backend(pg_backend_pid())");
          return ok(undefined);
        });
        await expect(killed).rejects.toBeInstanceOf(StoreError);
        expect(unwrap(await f.store.read(ROOT)).fold.seq).toBe(1);
        unwrap(await w.append([userInput("b"), turnCompleted]));
        expect(unwrap(await f.store.read(ROOT)).fold.seq).toBe(3);
      } finally {
        await f.close();
      }
    },
  );
});

describe("the same bytes on both engines", () => {
  pgTest(
    "a SQLite export imports into Postgres and back, byte for byte",
    async () => {
      const f = await pgFixture();
      const sqliteDb = openBunSqlite(":memory:");
      try {
        const lite = unwrap(
          await LogStore.open(sqliteDb, () => T0, memoryArtifacts()),
        );
        unwrap(await lite.createBranch(THREAD, ROOT));
        const w = unwrap(await lite.acquire(ROOT, "w"));
        unwrap(await w.append([started, userInput("hi"), turnCompleted]));
        const exported = unwrap(await lite.exportBranch(ROOT));
        unwrap(await f.store.importLog(exported));
        const back = unwrap(await f.store.exportBranch(ROOT));
        expect(back).toEqual(exported);
        const again = unwrap(
          await LogStore.open(
            openBunSqlite(":memory:"),
            () => T0,
            memoryArtifacts(),
          ),
        );
        unwrap(await again.importLog(back));
        expect(unwrap(await again.exportBranch(ROOT))).toEqual(exported);
      } finally {
        await sqliteDb.close();
        await f.close();
      }
    },
  );
});
