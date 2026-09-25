import { describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { StoreError } from "../../src/store/driver";
import { rows, run } from "./helpers";

// Several host processes share one store file. A write that meets another process's
// transaction must wait for it, not fail with SQLITE_BUSY (found by the F9.6 two-writers drill).

describe("bun:sqlite driver", () => {
  test("waits out another connection's write lock instead of failing at once", async () => {
    const dir = mkdtempSync(join(tmpdir(), "threads-busy-"));
    const db = openBunSqlite(join(dir, "threads.db"));
    try {
      expect(await rows(db, "PRAGMA busy_timeout", [])).toEqual([
        { timeout: 5_000 },
      ]);
    } finally {
      await db.close();
      rmSync(dir, { recursive: true, force: true });
    }
  });

  // fullfsync only acts on darwin, but it is set everywhere so this runs on every platform.
  test("a file store is opened for durable commits", async () => {
    const dir = mkdtempSync(join(tmpdir(), "threads-durable-"));
    const db = openBunSqlite(join(dir, "threads.db"));
    try {
      const read = (pragma: string) => rows(db, `PRAGMA ${pragma}`, []);
      expect(await read("journal_mode")).toEqual([{ journal_mode: "wal" }]);
      expect(await read("synchronous")).toEqual([{ synchronous: 2 }]);
      expect(await read("fullfsync")).toEqual([{ fullfsync: 1 }]);
      expect(await read("checkpoint_fullfsync")).toEqual([
        { checkpoint_fullfsync: 1 },
      ]);
    } finally {
      await db.close();
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("an outage is a StoreError; a SQL bug is not", async () => {
    // Can't open: an outage a later try may not meet.
    expect(() => openBunSqlite("/nonexistent/threads/threads.db")).toThrow(
      StoreError,
    );
    const db = openBunSqlite(":memory:");
    try {
      await run(db, "CREATE TABLE t (x INTEGER PRIMARY KEY)");
      await run(db, "INSERT INTO t (x) VALUES (?)", [1]);
      const bugs = [
        () => run(db, "SELEC 1"),
        () => run(db, "INSERT INTO t (x) VALUES (?)", [1]),
        () => rows(db, "SELECT nope FROM t", []),
      ];
      for (const bug of bugs) {
        const thrown = await bug().then(
          () => undefined,
          (error: unknown) => error,
        );
        expect(thrown).toBeInstanceOf(Error);
        expect(thrown).not.toBeInstanceOf(StoreError);
      }
    } finally {
      await db.close();
    }
  });
});
