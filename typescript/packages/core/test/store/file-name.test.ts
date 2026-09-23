import { expect, test } from "bun:test";
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { openStore, sqlite } from "../../src/agent/sqlite";

// one file, <home>/threads.db, the same name the Python runtime opens, so both
// runtimes share a store directory.
test("a store directory keeps its log in threads.db", async () => {
  const dir = mkdtempSync(join(tmpdir(), "threads-store-"));
  await openStore(sqlite(dir));
  expect(existsSync(join(dir, "threads.db"))).toBe(true);
});
