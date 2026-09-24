import { Database } from "bun:sqlite";
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { LOCAL_TENANT, LogStore, memoryArtifacts } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { fixture, ROOT, THREAD, unwrap } from "./helpers";

const STORE_SQL = readFileSync(
  join(import.meta.dir, "../../../../../spec/schema/store.sql"),
  "utf8",
);

function schema(db: Database): unknown[] {
  return db
    .query("SELECT type, name, sql FROM sqlite_master ORDER BY name")
    .all();
}

describe("store schema", () => {
  test("a fresh store has exactly the spec/schema/store.sql tables", () => {
    const f = fixture();
    unwrap(f.store.createBranch(THREAD, ROOT));
    const spec = new Database(":memory:");
    spec.exec(STORE_SQL);
    expect(
      f.db.all("SELECT type, name, sql FROM sqlite_master ORDER BY name", []),
    ).toEqual(schema(spec));
    expect(f.db.all("PRAGMA user_version", [])).toEqual([{ user_version: 4 }]);
    expect(f.db.all("SELECT thread_id, tenant_id FROM threads", [])).toEqual([
      { thread_id: THREAD, tenant_id: LOCAL_TENANT },
    ]);
    expect(f.db.all("SELECT tenant_id FROM branches", [])).toEqual([
      { tenant_id: LOCAL_TENANT },
    ]);
  });

  test("a database with a newer schema is unsupported_format", () => {
    const db = openBunSqlite(":memory:");
    db.exec("PRAGMA user_version = 5");
    const opened = LogStore.open(db, () => 0, memoryArtifacts());
    expect(opened.ok ? "ok" : opened.error.code).toBe("unsupported_format");
  });
});
