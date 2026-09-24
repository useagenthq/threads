import { Database, SQLiteError } from "bun:sqlite";
import { outage, type SqliteDriver, StoreError } from "./driver";

/**
 * A SqliteDriver on `bun:sqlite`: WAL, synchronous=FULL, and full fsync (acted on only by
 * darwin), so a committed append is durable when the call returns. `":memory:"` gives an
 * in-memory store for tests with the same code path.
 */
export function openBunSqlite(path: string): SqliteDriver {
  return guarded(() => open(path));
}

function open(path: string): SqliteDriver {
  const db = new Database(path, { create: true, strict: true });
  // Other processes may share the file: a write meeting theirs waits, never fails SQLITE_BUSY.
  db.exec("PRAGMA busy_timeout = 5000");
  walMode(db);
  db.exec("PRAGMA synchronous = FULL");
  db.exec("PRAGMA foreign_keys = ON");
  // Only darwin acts on these; set everywhere, as Python does, so one test reads them back.
  db.exec("PRAGMA fullfsync = ON");
  db.exec("PRAGMA checkpoint_fullfsync = ON");
  return {
    exec: (sql) => {
      guarded(() => db.exec(sql));
    },
    run: (sql, params) => {
      guarded(() => db.query(sql).run(...params));
    },
    all: (sql, params) => guarded(() => db.query(sql).all(...params)),
    // The body's own errors pass through; only SQLite's become StoreError.
    transaction: (fn) => guarded(() => db.transaction(fn).immediate()),
    close: () => {
      guarded(() => db.close());
    },
  };
}

/** SQLite's outages, raised as StoreError: what a host may try again later. A bug stays itself. */
function guarded<T>(run: () => T): T {
  try {
    return run();
  } catch (error) {
    if (
      error instanceof SQLiteError &&
      typeof error.code === "string" &&
      outage(error.code)
    )
      throw new StoreError(error.message, { cause: error });
    throw error;
  }
}

const WAL_TRIES = 500;

/**
 * Switching a new file to WAL can meet another process doing the same, and SQLite answers that
 * SQLITE_BUSY without calling its busy handler: retried, so two hosts can open one fresh store.
 */
function walMode(db: Database): void {
  for (let tried = 1; ; tried += 1) {
    try {
      db.exec("PRAGMA journal_mode = WAL");
      return;
    } catch (error) {
      const busy =
        error instanceof Error &&
        "code" in error &&
        error.code === "SQLITE_BUSY";
      if (!busy || tried >= WAL_TRIES) throw error;
      Bun.sleepSync(10);
    }
  }
}
