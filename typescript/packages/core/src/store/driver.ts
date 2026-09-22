export type SqlValue = string | number | Uint8Array | null;

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
