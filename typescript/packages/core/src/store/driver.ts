import type { Result } from "../result";
import type { LogError } from "../verify/error";

export type SqlValue = string | number | Uint8Array | null;

/**
 * An outage of the store itself (SQLite busy, locked, out of space, an I/O error; a lost
 * Postgres connection, a serialization failure that outlived its retries; the disk under the
 * artifacts), thrown where the store meets them: a later try may not meet it again. A SQL bug
 * (a syntax error, a constraint) is never one: it throws as itself. Internal: exported from
 * `@threads/core/host` only.
 */
export class StoreError extends Error {
  override readonly name: string = "StoreError";
}

/**
 * `COMMIT` itself failed, so what it wrote is unknown (Postgres only; SQLite never has this
 * state). A writer that meets it is poisoned and reloaded from the log; every other write the
 * store makes is keyed or conditional, so doing it again is a no-op.
 */
export class CommitUnknown extends StoreError {
  override readonly name: string = "CommitUnknown";
}

/** SQLite's result codes for an outage (extended codes share their prefix); Python uses the same. */
const OUTAGES = [
  "SQLITE_BUSY",
  "SQLITE_LOCKED",
  "SQLITE_IOERR",
  "SQLITE_FULL",
  "SQLITE_CANTOPEN",
  "SQLITE_NOMEM",
  "SQLITE_READONLY",
  "SQLITE_CORRUPT",
  "SQLITE_NOTADB",
  "SQLITE_PROTOCOL",
] as const;

export function outage(code: string): boolean {
  return OUTAGES.some((prefix) => code.startsWith(prefix));
}

/**
 * One transaction's statements. Only `tx` is awaited inside a transaction: no model, sandbox,
 * hook, network or timer I/O. A function the store runs in one may be run again from the start
 * (a Postgres serialization failure), so it reads and writes only through `tx` and its own
 * locals, and builds everything it returns inside the attempt.
 */
export type Tx = {
  readonly dialect: Dialect;
  /** Runs a statement; resolves to the number of rows it changed. */
  readonly run: (sql: string, params?: readonly SqlValue[]) => Promise<number>;
  /** Rows as the driver returns them: unknown until parsed (storage is a boundary). */
  readonly all: (
    sql: string,
    params?: readonly SqlValue[],
  ) => Promise<readonly unknown[]>;
  /** A nested transaction: a savepoint, rolled back alone when `fn` throws. */
  readonly transaction: <T>(fn: (tx: Tx) => Promise<T>) => Promise<T>;
  /** Runs `fn` once the outermost transaction has committed (never after a rollback or retry). */
  readonly afterCommit: (fn: () => void) => void;
};

export type Dialect = "sqlite" | "postgres";

export type TransactionOptions = {
  /** A read-only transaction takes no write lock (SQLite DEFERRED, Postgres READ ONLY). */
  readonly readOnly?: boolean;
};

/**
 * The store's connection: every statement runs inside `transaction`, never alone. A write is
 * one IMMEDIATE (SQLite) or SERIALIZABLE (Postgres) transaction; a throw rolls it back. Internal:
 * `@threads/core/store-driver` exports it for the in-repo Postgres package only.
 */
export type StoreDriver = {
  readonly dialect: Dialect;
  readonly transaction: <T>(
    fn: (tx: Tx) => Promise<T>,
    options?: TransactionOptions,
  ) => Promise<T>;
  /** Creates the store's tables in an empty database; refuses one of another version. */
  readonly install: () => Promise<Result<void, LogError>>;
  readonly close: () => Promise<void>;
};

/** What a statement function needs: a transaction, or a driver it opens one on. */
export type Sql = StoreDriver | Tx;

/** Runs `fn` in a transaction of its own on a driver, or in a savepoint of an open one. */
export function writing<T>(
  sql: Sql,
  fn: (tx: Tx) => Promise<T>,
  options?: TransactionOptions,
): Promise<T> {
  return "afterCommit" in sql
    ? sql.transaction(fn)
    : sql.transaction(fn, options);
}

/** A read on a driver (a read-only transaction) or inside an open transaction. */
export function reading<T>(sql: Sql, fn: (tx: Tx) => Promise<T>): Promise<T> {
  return "afterCommit" in sql ? fn(sql) : sql.transaction(fn, READ_ONLY);
}

export const READ_ONLY: TransactionOptions = { readOnly: true };
