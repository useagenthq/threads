import { Database, SQLiteError } from "bun:sqlite";
import { err, ok, type Result } from "../result";
import { type LogError, logError } from "../verify/error";
import {
  outage,
  type StoreDriver,
  StoreError,
  type TransactionOptions,
  type Tx,
} from "./driver";
import { STORE_SQL, STORE_VERSION } from "./generated/sql";
import { type Commits, Fifo, guarded, openTx, type Statements } from "./tx";

/**
 * A StoreDriver on `bun:sqlite`: WAL, synchronous=FULL, and full fsync (acted on only by
 * darwin), so a committed append is durable when the call returns. `":memory:"` gives an
 * in-memory store for tests with the same code path. One transaction runs at a time on the
 * connection; a write is BEGIN IMMEDIATE, a read BEGIN DEFERRED (no write lock, so readers never
 * contend with writers).
 */
export function openBunSqlite(path: string): StoreDriver {
  const db = sqlite(() => new Database(path, { create: true, strict: true }));
  sqlite(() => configure(db));
  const statements: Statements = {
    run: async (sql, params) =>
      sqlite(() => db.query(sql).run(...params)).changes,
    all: async (sql, params) => sqlite(() => db.query(sql).all(...params)),
  };
  const fifo = new Fifo();
  const driver: StoreDriver = {
    dialect: "sqlite",
    transaction: (fn, options) =>
      guarded(driver, async () => {
        const release = await fifo.turn();
        try {
          return await attempt(db, statements, fn, options);
        } finally {
          release();
        }
      }),
    install: async () => {
      const release = await fifo.turn();
      try {
        return sqlite(() => install(db));
      } finally {
        release();
      }
    },
    close: async () => {
      const release = await fifo.turn();
      try {
        sqlite(() => db.close());
      } finally {
        release();
      }
    },
  };
  return driver;
}

async function attempt<T>(
  db: Database,
  statements: Statements,
  fn: (tx: Tx) => Promise<T>,
  options: TransactionOptions | undefined,
): Promise<T> {
  const readOnly = options?.readOnly === true;
  sqlite(() => db.exec(readOnly ? "BEGIN DEFERRED" : "BEGIN IMMEDIATE"));
  const commits: Commits = [];
  let done: T;
  try {
    done = await fn(openTx(statements, "sqlite", readOnly, commits));
  } catch (error) {
    if (db.inTransaction) sqlite(() => db.exec("ROLLBACK"));
    throw error;
  }
  sqlite(() => db.exec("COMMIT"));
  for (const hook of commits) hook();
  return done;
}

function configure(db: Database): void {
  // Other processes may share the file: a write meeting theirs waits, never fails SQLITE_BUSY.
  db.exec("PRAGMA busy_timeout = 5000");
  walMode(db);
  db.exec("PRAGMA synchronous = FULL");
  db.exec("PRAGMA foreign_keys = ON");
  // Only darwin acts on these; set everywhere, as Python does, so one test reads them back.
  db.exec("PRAGMA fullfsync = ON");
  db.exec("PRAGMA checkpoint_fullfsync = ON");
}

/** The oldest SQLite with IS NOT DISTINCT FROM, which the portable statements use. */
const MIN_SQLITE = [3, 39, 0] as const;

/**
 * Creates the store.sql tables on a new database. A database a newer schema wrote is refused,
 * never downgraded; one an earlier version wrote is refused too, since stores are not migrated.
 */
function install(db: Database): Result<void, LogError> {
  const version = sqliteVersion(db);
  if (version !== undefined) return err(version);
  const row = db.query("PRAGMA user_version").get();
  const found =
    typeof row === "object" &&
    row !== null &&
    "user_version" in row &&
    typeof row.user_version === "number"
      ? row.user_version
      : 0;
  if (found > STORE_VERSION)
    return err(
      logError(
        "unsupported_format",
        `store schema ${found} is newer than ${STORE_VERSION}`,
      ),
    );
  if (found !== 0 && found < STORE_VERSION)
    return err(
      logError(
        "unsupported_format",
        `this store was created by an earlier threads version (schema ${found}); create a new store`,
      ),
    );
  db.exec(STORE_SQL);
  return ok(undefined);
}

function sqliteVersion(db: Database): LogError | undefined {
  const row = db.query("SELECT sqlite_version() AS v").get();
  const text =
    typeof row === "object" &&
    row !== null &&
    "v" in row &&
    typeof row.v === "string"
      ? row.v
      : "0";
  const parts = text.split(".").map(Number);
  for (const [i, min] of MIN_SQLITE.entries()) {
    const part = parts[i] ?? 0;
    if (part > min) return undefined;
    if (part < min)
      return logError(
        "unsupported_format",
        `SQLite ${text} is older than 3.39, which threads needs`,
      );
  }
  return undefined;
}

/** SQLite's outages, raised as StoreError: what a host may try again later. A bug stays itself. */
function sqlite<T>(run: () => T): T {
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
