import { describe, expect, test } from "bun:test";
import { ok } from "../../src/result";
import {
  fixture,
  ROOT,
  rows,
  started,
  THREAD,
  turnCompleted,
  unwrap,
  userInput,
} from "./helpers";

// The async store seam (lane 27A) on the engine under test: one writer's appends queue behind
// its lock, each against the committed head; a nested transaction is a savepoint.

describe("one writer, many callers", () => {
  test("50 concurrent appends commit once each, at contiguous seqs, and nothing is poisoned", async () => {
    const { db, store } = await fixture();
    try {
      unwrap(await store.createBranch(THREAD, ROOT));
      const writer = unwrap(await store.acquire(ROOT, "w"));
      unwrap(await writer.append([started]));
      const done = await Promise.all(
        Array.from({ length: 50 }, (_, i) =>
          writer.append([userInput(`m${i}`), turnCompleted]),
        ),
      );
      expect(done.filter((d) => !d.ok)).toEqual([]);
      const seqs = unwrap(await store.read(ROOT)).events.map((e) =>
        e.kind === "event" ? e.event.seq : 0,
      );
      expect(seqs).toEqual(Array.from({ length: 101 }, (_, i) => i + 1));
      unwrap(await writer.append([userInput("after")]));
    } finally {
      await db.close();
    }
  });

  test("a decided append whose nested transaction rolls back commits only its own rows", async () => {
    const { db, store } = await fixture();
    try {
      unwrap(await store.createBranch(THREAD, ROOT));
      const writer = unwrap(await store.acquire(ROOT, "w"));
      const done = await writer.appendDecided(async ({ tx }) => {
        await tx.run(
          "INSERT INTO tombstones (thread_id, tenant_id, deleted_at) VALUES (?, ?, ?)",
          ["kept", "local", 1],
        );
        await tx
          .transaction(async (inner) => {
            await inner.run(
              "INSERT INTO tombstones (thread_id, tenant_id, deleted_at) VALUES (?, ?, ?)",
              ["rolled back", "local", 1],
            );
            throw new Error("only the savepoint");
          })
          .catch(() => undefined);
        return ok([started]);
      });
      expect("kind" in done ? "refused" : done.ok).toBe(true);
      expect(
        await rows(db, "SELECT thread_id FROM tombstones ORDER BY thread_id"),
      ).toEqual([{ thread_id: "kept" }]);
    } finally {
      await db.close();
    }
  });

  test("a transaction opened inside another on the same store is refused, never a deadlock", async () => {
    const { db } = await fixture();
    try {
      const nested = db.transaction(() =>
        db.transaction((tx) => tx.all("SELECT 1 AS one")),
      );
      await expect(nested).rejects.toThrow("nest through tx.transaction");
    } finally {
      await db.close();
    }
  });

  test("a write inside a read-only transaction is a bug", async () => {
    const { db } = await fixture();
    try {
      const wrote = db.transaction((tx) => tx.run("DELETE FROM tombstones"), {
        readOnly: true,
      });
      await expect(wrote).rejects.toThrow("read-only transaction");
    } finally {
      await db.close();
    }
  });
});
