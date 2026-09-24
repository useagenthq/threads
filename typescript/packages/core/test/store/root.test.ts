import { expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { BranchId, ThreadId } from "../../src/log";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { fixture, unwrap } from "./helpers";

// rootOrCreate: two connections to one store file (two processes) starting the same thread get
// the one root that stood first; the second never makes a branch of its own.

const THREAD = ThreadId.parse("0192e000-0000-7000-8000-00000000a001");
const FIRST = BranchId.parse("0192e000-0000-7000-8000-00000000b001");
const SECOND = BranchId.parse("0192e000-0000-7000-8000-00000000b002");

test("two connections starting one thread get its first root", () => {
  const path = join(mkdtempSync(join(tmpdir(), "root-")), "log.db");
  const a = fixture("acme", openBunSqlite(path));
  const b = fixture("acme", openBunSqlite(path));
  expect(unwrap(a.store.rootOrCreate(THREAD, FIRST))).toBe(FIRST);
  expect(unwrap(b.store.rootOrCreate(THREAD, SECOND))).toBe(FIRST);
  expect(unwrap(b.store.mainBranch(THREAD))).toBe(FIRST);
  expect(
    b.db.all("SELECT branch_id FROM branches WHERE thread_id = ?", [THREAD]),
  ).toEqual([{ branch_id: FIRST }]);
});
