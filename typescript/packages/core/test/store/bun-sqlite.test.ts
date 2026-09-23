import { describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { openBunSqlite } from "../../src/store/bun-sqlite";

// Several host processes share one store file. A write that meets another process's
// transaction must wait for it, not fail with SQLITE_BUSY (found by the F9.6 two-writers drill).

describe("bun:sqlite driver", () => {
  test("waits out another connection's write lock instead of failing at once", () => {
    const dir = mkdtempSync(join(tmpdir(), "threads-busy-"));
    const db = openBunSqlite(join(dir, "threads.db"));
    try {
      expect(db.all("PRAGMA busy_timeout", [])).toEqual([{ timeout: 5_000 }]);
    } finally {
      db.close();
      rmSync(dir, { recursive: true, force: true });
    }
  });

  // fullfsync only acts on darwin, but it is set everywhere so this runs on every platform.
  test("a file store is opened for durable commits", () => {
    const dir = mkdtempSync(join(tmpdir(), "threads-durable-"));
    const db = openBunSqlite(join(dir, "threads.db"));
    try {
      const read = (pragma: string) => db.all(`PRAGMA ${pragma}`, []);
      expect(read("journal_mode")).toEqual([{ journal_mode: "wal" }]);
      expect(read("synchronous")).toEqual([{ synchronous: 2 }]);
      expect(read("fullfsync")).toEqual([{ fullfsync: 1 }]);
      expect(read("checkpoint_fullfsync")).toEqual([
        { checkpoint_fullfsync: 1 },
      ]);
    } finally {
      db.close();
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
