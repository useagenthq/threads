export type SqlValue = string | number | Uint8Array | null;

/**
 * An outage of the store itself (SQLite busy, locked, out of space, an I/O error; the disk under
 * its artifacts), thrown where the store meets them: a later try may not meet it again. A SQL
 * bug (a syntax error, a constraint) is never one: it throws as itself. Internal: exported from
 * `@threads/core/host` only.
 */
export class StoreError extends Error {
  override readonly name = "StoreError";
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
 * The few SQLite calls the store makes. Core depends on no SQLite binding: `bun-sqlite.ts`
 * implements this with `bun:sqlite`, and a `node:sqlite` driver can be added beside it.
 */
export type SqliteDriver = {
  readonly exec: (sql: string) => void;
  readonly run: (sql: string, params: readonly SqlValue[]) => void;
  /** Rows as the binding returns them: unknown until parsed (storage is a boundary). */
  readonly all: (
    sql: string,
    params: readonly SqlValue[],
  ) => readonly unknown[];
  /** Runs `fn` in one IMMEDIATE transaction; a throw rolls back. Nested calls are savepoints. */
  readonly transaction: <T>(fn: () => T) => T;
  readonly close: () => void;
};
