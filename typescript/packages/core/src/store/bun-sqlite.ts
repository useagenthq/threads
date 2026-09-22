import { Database } from "bun:sqlite";
import type { SqliteDriver } from "./driver";

/**
 * A SqliteDriver on `bun:sqlite`: WAL, synchronous=FULL, and full fsync on
 * darwin, so a committed append is durable when the call returns. `":memory:"` gives an
 * in-memory store for tests with the same code path.
 */
export function openBunSqlite(path: string): SqliteDriver {
  const db = new Database(path, { create: true, strict: true });
  db.exec("PRAGMA journal_mode = WAL");
  db.exec("PRAGMA synchronous = FULL");
  db.exec("PRAGMA foreign_keys = ON");
  if (process.platform === "darwin") {
    db.exec("PRAGMA fullfsync = ON");
    db.exec("PRAGMA checkpoint_fullfsync = ON");
  }
  return {
    exec: (sql) => {
      db.exec(sql);
    },
    run: (sql, params) => {
      db.query(sql).run(...params);
    },
    all: (sql, params) => db.query(sql).all(...params),
    transaction: (fn) => db.transaction(fn).immediate(),
    close: () => {
      db.close();
    },
  };
}
