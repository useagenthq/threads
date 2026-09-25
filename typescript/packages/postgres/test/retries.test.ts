import { describe, expect } from "bun:test";
import { CommitUnknown, StoreError } from "@threads/core/store-driver";
import { ok } from "../../core/src/result";
import { pgTest } from "./kit";
import {
  pgFixture,
  ROOT,
  started,
  THREAD,
  turnCompleted,
  unwrap,
  userInput,
} from "./store-kit";

// The retry-safety rule and commit_unknown (spec/schema/README.md, "Storage"), forced at the
// worst point: COMMIT, after admission, decide and alongside have all run.

type Fault = "retry" | "lost" | "after";

async function opened(plan: Fault[]) {
  // A short budget: a drill that always conflicts ends in 100 ms, not the 5 s default.
  const f = await pgFixture({
    atCommit: () => plan.shift(),
    retryBudgetMs: 100,
  });
  unwrap(await f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(await f.store.acquire(ROOT, "w"));
  unwrap(await writer.append([started]));
  return { ...f, writer };
}

describe("a serialization failure re-runs the whole attempt", () => {
  pgTest("once: the append commits once, and the writer goes on", async () => {
    const plan: Fault[] = [];
    const f = await opened(plan);
    try {
      let alongside = 0;
      plan.push("retry");
      const added = unwrap(
        await f.writer.append([userInput("a")], async () => {
          alongside += 1;
          return ok(undefined);
        }),
      );
      expect(added.map((e) => e.kind === "event" && e.event.seq)).toEqual([2]);
      expect(alongside).toBe(2);
      expect(f.db.retries()).toBe(1);
      unwrap(await f.writer.append([turnCompleted]));
      const log = unwrap(await f.store.read(ROOT));
      expect(log.events.map((e) => e.kind === "event" && e.event.seq)).toEqual([
        1, 2, 3,
      ]);
    } finally {
      await f.close();
    }
  });

  pgTest("a decided append decides again on each attempt", async () => {
    const plan: Fault[] = [];
    const f = await opened(plan);
    try {
      let decided = 0;
      plan.push("retry");
      const done = await f.writer.appendDecided(async ({ tx }) => {
        decided += 1;
        await tx.all("SELECT 1 AS one");
        return ok([userInput("a")]);
      });
      expect("kind" in done ? "refused" : done.ok).toBe(true);
      expect(decided).toBe(2);
    } finally {
      await f.close();
    }
  });

  pgTest(
    "past the retry budget: StoreError, nothing written, the writer still usable",
    async () => {
      const plan: Fault[] = [];
      const f = await opened(plan);
      try {
        plan.push(...Array.from({ length: 10_000 }, () => "retry" as const));
        await expect(f.writer.append([userInput("a")])).rejects.toBeInstanceOf(
          StoreError,
        );
        // More than the old four attempts fit in the budget.
        expect(f.db.retries()).toBeGreaterThan(3);
        plan.length = 0;
        expect(unwrap(await f.store.read(ROOT)).fold.seq).toBe(1);
        unwrap(await f.writer.append([userInput("b")]));
        expect(unwrap(await f.store.read(ROOT)).fold.seq).toBe(2);
      } finally {
        await f.close();
      }
    },
  );
});

describe("a commit whose outcome is unknown", () => {
  pgTest(
    "committed: the writer is poisoned, and a reload sees the append once",
    async () => {
      const plan: Fault[] = [];
      const f = await opened(plan);
      try {
        plan.push("after");
        await expect(f.writer.append([userInput("a")])).rejects.toBeInstanceOf(
          CommitUnknown,
        );
        const again = await f.writer.append([userInput("b")]);
        expect(again.ok ? "ok" : again.error.code).toBe("writer_poisoned");
        f.clock.now += 60_000;
        const reloaded = unwrap(await f.store.acquire(ROOT, "w2"));
        expect(reloaded.chain.fold.seq).toBe(2);
        unwrap(await reloaded.append([turnCompleted]));
        expect(unwrap(await f.store.read(ROOT)).fold.seq).toBe(3);
      } finally {
        await f.close();
      }
    },
  );

  pgTest(
    "lost before COMMIT: nothing written, and the reload continues",
    async () => {
      const plan: Fault[] = [];
      const f = await opened(plan);
      try {
        plan.push("lost");
        await expect(f.writer.append([userInput("a")])).rejects.toBeInstanceOf(
          CommitUnknown,
        );
        f.clock.now += 60_000;
        const reloaded = unwrap(await f.store.acquire(ROOT, "w2"));
        expect(reloaded.chain.fold.seq).toBe(1);
      } finally {
        await f.close();
      }
    },
  );
});

describe("one writer", () => {
  pgTest(
    "50 concurrent appends commit once each, at contiguous seqs",
    async () => {
      const f = await opened([]);
      try {
        const done = await Promise.all(
          Array.from({ length: 50 }, (_, i) =>
            f.writer.append([userInput(`m${i}`), turnCompleted]),
          ),
        );
        expect(done.every((d) => d.ok)).toBe(true);
        const seqs = unwrap(await f.store.read(ROOT)).events.map(
          (e) => e.kind === "event" && e.event.seq,
        );
        expect(seqs).toEqual(Array.from({ length: 101 }, (_, i) => i + 1));
      } finally {
        await f.close();
      }
    },
  );

  pgTest(
    "a nested savepoint that rolls back leaves the outer append",
    async () => {
      const f = await opened([]);
      try {
        const done = await f.writer.appendDecided(async ({ tx }) => {
          try {
            await tx.transaction(async (inner) => {
              await inner.run(
                "INSERT INTO tombstones (thread_id, tenant_id, deleted_at) VALUES (?, ?, ?)",
                ["gone", "local", 1],
              );
              throw new Error("roll the savepoint back");
            });
          } catch {
            // the savepoint only
          }
          return ok([userInput("kept")]);
        });
        expect("kind" in done ? "refused" : done.ok).toBe(true);
        const rows = await f.db.transaction((tx) =>
          tx.all("SELECT thread_id FROM tombstones"),
        );
        expect(rows).toEqual([]);
        expect(unwrap(await f.store.read(ROOT)).fold.seq).toBe(2);
      } finally {
        await f.close();
      }
    },
  );

  pgTest(
    "a write inside a read-only transaction is a bug, not an outage",
    async () => {
      const f = await pgFixture();
      try {
        const wrote = f.db.transaction(
          (tx) => tx.run("DELETE FROM tombstones"),
          { readOnly: true },
        );
        await expect(wrote).rejects.toThrow("read-only transaction");
      } finally {
        await f.close();
      }
    },
  );
});
