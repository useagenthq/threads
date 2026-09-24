import { Database } from "bun:sqlite";
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { LOCAL_TENANT, LogStore, memoryArtifacts } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { STORE_VERSION } from "../../src/store/generated/sql";
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
    expect(f.db.all("PRAGMA user_version", [])).toEqual([
      { user_version: STORE_VERSION },
    ]);
    expect(f.db.all("SELECT thread_id, tenant_id FROM threads", [])).toEqual([
      { thread_id: THREAD, tenant_id: LOCAL_TENANT },
    ]);
    expect(f.db.all("SELECT tenant_id FROM branches", [])).toEqual([
      { tenant_id: LOCAL_TENANT },
    ]);
  });

  test("a database with a newer schema is unsupported_format", () => {
    const db = openBunSqlite(":memory:");
    db.exec(`PRAGMA user_version = ${STORE_VERSION + 1}`);
    const opened = LogStore.open(db, () => 0, memoryArtifacts());
    expect(opened.ok ? "ok" : opened.error.code).toBe("unsupported_format");
  });

  test("a store an earlier version created is refused with the recreate message", () => {
    const db = openBunSqlite(":memory:");
    db.exec(
      "CREATE TABLE threads (thread_id TEXT PRIMARY KEY); PRAGMA user_version = 1",
    );
    const opened = LogStore.open(db, () => 0, memoryArtifacts());
    expect(opened.ok ? "ok" : opened.error).toEqual({
      code: "unsupported_format",
      message:
        "this store was created by an earlier threads version (schema 1); create a new store",
    });
    // Nothing of the new layout was installed on it.
    expect(
      db.all(
        "SELECT name FROM sqlite_master WHERE name = 'schedule_threads'",
        [],
      ),
    ).toEqual([]);
  });

  test("every older version is refused, and gets none of the current tables", () => {
    for (let version = 1; version < STORE_VERSION; version++) {
      const db = openBunSqlite(":memory:");
      db.exec(
        `CREATE TABLE threads (thread_id TEXT PRIMARY KEY); PRAGMA user_version = ${version}`,
      );
      const opened = LogStore.open(db, () => 0, memoryArtifacts());
      expect(opened.ok ? "ok" : opened.error.message).toBe(
        `this store was created by an earlier threads version (schema ${version}); create a new store`,
      );
      expect(
        db.all("SELECT name FROM sqlite_master WHERE name = 'observers'", []),
      ).toEqual([]);
    }
  });

  test("the pending sweep reads the partial index, and removed is a stored reason", () => {
    const f = fixture();
    const plan = f.db.all(
      `EXPLAIN QUERY PLAN SELECT schedule_id FROM schedule_occurrences
        WHERE tenant_id = ? AND state = 'pending' ORDER BY occurrence_at, thread_id, schedule_id`,
      ["local"],
    );
    expect(JSON.stringify(plan)).toContain("schedule_occurrences_pending");
    f.db.run(
      `INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at, state, reason,
        thread_id, claimed_at, logged_seq) VALUES ('local', 'daily', 1, 'skipped', 'removed', ?, 1, 2)`,
      [THREAD],
    );
    expect(() =>
      f.db.run(
        `INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at, state,
          thread_id, claimed_at) VALUES ('local', 'daily', 2, 'pending', ?, 1)`,
        [THREAD],
      ),
    ).toThrow();
  });
});
