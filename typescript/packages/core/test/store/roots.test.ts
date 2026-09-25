import { describe, expect, test } from "bun:test";
import { LogStore, type StoreDriver } from "../../src/store";
import { CHILD, fixture, ROOT, rows, started, THREAD, unwrap } from "./helpers";

// A thread has one root branch (spec/schema/store.sql, branches_root): hosts racing to create
// one thread, on the engine under test, leave one root and refuse the rest as values.

const LEASE = { holderId: "opener", ttlMs: 30_000 };

async function roots(db: StoreDriver): Promise<unknown> {
  return rows(
    db,
    "SELECT branch_id FROM branches WHERE thread_id = ? AND parent_branch_id IS NULL",
    [THREAD],
  );
}

describe("one root per thread", () => {
  test("two hosts racing createBranch for one thread: one root, the other refused", async () => {
    const { db, store, clock, artifacts } = await fixture();
    try {
      const other = unwrap(await LogStore.open(db, () => clock.now, artifacts));
      const made = await Promise.all([
        store.createBranch(THREAD, ROOT),
        other.createBranch(THREAD, CHILD),
      ]);
      expect(made.map((m) => (m.ok ? "ok" : m.error.code)).toSorted()).toEqual([
        "invalid_transition",
        "ok",
      ]);
      const winner = made[0]?.ok ? ROOT : CHILD;
      expect(await roots(db)).toEqual([{ branch_id: winner }]);
      expect(unwrap(await other.mainBranch(THREAD))).toBe(winner);
    } finally {
      await db.close();
    }
  });

  test("two hosts racing openBranch for one thread: one writer, the other refused", async () => {
    const { db, store, clock, artifacts } = await fixture();
    try {
      const other = unwrap(await LogStore.open(db, () => clock.now, artifacts));
      const opened = await Promise.all(
        [
          { s: store, branchId: ROOT },
          { s: other, branchId: CHILD },
        ].map(({ s, branchId }) =>
          s.openBranch({
            threadId: THREAD,
            branchId,
            lease: LEASE,
            drafts: [started],
          }),
        ),
      );
      expect(
        opened.map((o) => (o.ok ? "ok" : o.error.code)).toSorted(),
      ).toEqual(["invalid_transition", "ok"]);
      expect(await roots(db)).toHaveLength(1);
    } finally {
      await db.close();
    }
  });

  test("an import of another root of a stored thread is refused, and stores nothing", async () => {
    const source = await fixture();
    const target = await fixture();
    try {
      unwrap(await source.store.createBranch(THREAD, CHILD));
      const bytes = unwrap(await source.store.exportBranch(CHILD));
      unwrap(await target.store.createBranch(THREAD, ROOT));
      const imported = await target.store.importLog(bytes);
      expect(imported.ok ? "ok" : imported.error.code).toBe("branch_exists");
      expect(await roots(target.db)).toEqual([{ branch_id: ROOT }]);
    } finally {
      await source.db.close();
      await target.db.close();
    }
  });
});
