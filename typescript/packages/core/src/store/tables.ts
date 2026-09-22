import { z } from "zod";
import { BranchId, Int, PosInt, Sha256, ThreadId } from "../log";
import type { EnumOf, Strict } from "../log/zod-types";
import { err, ok, type Result } from "../result";
import type { ChainEvent } from "../verify";
import { type LogError, logError } from "../verify/error";
import type { SqliteDriver } from "./driver";

// . Each line is stored as its exact bytes; parent rows are referenced through
// parent_branch_id and fork_at_seq, never copied. UNIQUE (branch_id, event_id) is physical
// only: the resolved-chain rule (semantic rule 28) is checked by validate_next.
// ponytail: no tenant_id or threads table yet; add them with tenancy.
export const DDL = `
CREATE TABLE IF NOT EXISTS branches (
  branch_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  parent_branch_id TEXT REFERENCES branches (branch_id),
  fork_at_seq INTEGER,
  header_line BLOB NOT NULL,
  state TEXT NOT NULL,
  head_seq INTEGER NOT NULL,
  head_hash TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS events (
  branch_id TEXT NOT NULL REFERENCES branches (branch_id),
  seq INTEGER NOT NULL,
  event_id TEXT NOT NULL,
  type TEXT NOT NULL,
  type_version INTEGER NOT NULL,
  critical INTEGER NOT NULL,
  epoch INTEGER NOT NULL,
  line BLOB NOT NULL,
  PRIMARY KEY (branch_id, seq),
  UNIQUE (branch_id, event_id)
) STRICT;
CREATE TABLE IF NOT EXISTS leases (
  branch_id TEXT PRIMARY KEY REFERENCES branches (branch_id),
  holder_id TEXT NOT NULL,
  epoch INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
) STRICT;
`;

const BRANCH_STATES = [
  "forking",
  "ready",
  "inspection_only",
  "read_only",
  "corrupt",
  "fork_failed",
] as const;

const Bytes: z.ZodCustom<Uint8Array, Uint8Array> = z.instanceof(Uint8Array);

export const BranchRow: Strict<{
  branch_id: typeof BranchId;
  thread_id: typeof ThreadId;
  parent_branch_id: z.ZodNullable<typeof BranchId>;
  fork_at_seq: z.ZodNullable<typeof Int>;
  header_line: typeof Bytes;
  state: EnumOf<typeof BRANCH_STATES>;
  head_seq: typeof Int;
  head_hash: typeof Sha256;
}> = z.strictObject({
  branch_id: BranchId,
  thread_id: ThreadId,
  parent_branch_id: BranchId.nullable(),
  fork_at_seq: Int.nullable(),
  header_line: Bytes,
  state: z.enum(BRANCH_STATES),
  head_seq: Int,
  head_hash: Sha256,
});
export type BranchRow = z.infer<typeof BranchRow>;

export const LeaseRow: Strict<{
  holder_id: z.ZodString;
  epoch: typeof PosInt;
  expires_at: typeof Int;
}> = z.strictObject({ holder_id: z.string(), epoch: PosInt, expires_at: Int });
export type LeaseRow = z.infer<typeof LeaseRow>;

const LineRow: Strict<{ line: typeof Bytes }> = z.strictObject({ line: Bytes });

/** A row that fails its schema is corruption, reported, never trusted. */
function parseRows<T>(
  schema: z.ZodType<T>,
  rows: readonly unknown[],
): Result<readonly T[], LogError> {
  const parsed: T[] = [];
  for (const row of rows) {
    const result = schema.safeParse(row);
    if (!result.success)
      return err(logError("log_corrupt", "a stored row fails its schema"));
    parsed.push(result.data);
  }
  return ok(parsed);
}

export function getBranch(
  db: SqliteDriver,
  branchId: string,
): Result<BranchRow | undefined, LogError> {
  const rows = parseRows(
    BranchRow,
    db.all(
      `SELECT branch_id, thread_id, parent_branch_id, fork_at_seq, header_line, state,
        head_seq, head_hash FROM branches WHERE branch_id = ?`,
      [branchId],
    ),
  );
  return rows.ok ? ok(rows.value[0]) : rows;
}

export function insertBranch(db: SqliteDriver, row: BranchRow): void {
  db.run(
    `INSERT INTO branches (branch_id, thread_id, parent_branch_id, fork_at_seq, header_line,
      state, head_seq, head_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
    [
      row.branch_id,
      row.thread_id,
      row.parent_branch_id,
      row.fork_at_seq,
      row.header_line,
      row.state,
      row.head_seq,
      row.head_hash,
    ],
  );
}

/** Inserts a branch's own event rows and advances its head checkpoint. */
export function insertEvents(
  db: SqliteDriver,
  branchId: string,
  events: readonly ChainEvent[],
): void {
  for (const { event, bytes } of events) {
    db.run(
      `INSERT INTO events (branch_id, seq, event_id, type, type_version, critical, epoch, line)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
      [
        branchId,
        event.seq,
        event.event_id,
        event.type,
        event.type_version,
        event.critical ? 1 : 0,
        event.epoch,
        bytes,
      ],
    );
  }
  const last = events.at(-1);
  if (last === undefined) return;
  db.run(
    "UPDATE branches SET head_seq = ?, head_hash = ? WHERE branch_id = ?",
    [last.event.seq, last.hash, branchId],
  );
}

/** A branch's own event lines with seq ≤ `through`, in seq order. */
export function eventLines(
  db: SqliteDriver,
  branchId: string,
  through: number,
): Result<readonly Uint8Array[], LogError> {
  const rows = parseRows(
    LineRow,
    db.all(
      "SELECT line FROM events WHERE branch_id = ? AND seq <= ? ORDER BY seq",
      [branchId, through],
    ),
  );
  return rows.ok ? ok(rows.value.map((row) => row.line)) : rows;
}

export function getLease(
  db: SqliteDriver,
  branchId: string,
): Result<LeaseRow | undefined, LogError> {
  const rows = parseRows(
    LeaseRow,
    db.all(
      "SELECT holder_id, epoch, expires_at FROM leases WHERE branch_id = ?",
      [branchId],
    ),
  );
  return rows.ok ? ok(rows.value[0]) : rows;
}

export function putLease(
  db: SqliteDriver,
  branchId: string,
  lease: LeaseRow,
): void {
  db.run(
    `INSERT INTO leases (branch_id, holder_id, epoch, expires_at) VALUES (?, ?, ?, ?)
      ON CONFLICT (branch_id) DO UPDATE SET holder_id = excluded.holder_id,
      epoch = excluded.epoch, expires_at = excluded.expires_at`,
    [branchId, lease.holder_id, lease.epoch, lease.expires_at],
  );
}

class Rollback extends Error {
  constructor(readonly error: LogError) {
    super(error.message);
  }
}

/** Runs `fn` in one transaction and rolls it back when `fn` returns an error value. */
export function atomically<T>(
  db: SqliteDriver,
  fn: () => Result<T, LogError>,
): Result<T, LogError> {
  try {
    return db.transaction(() => {
      const result = fn();
      if (!result.ok) throw new Rollback(result.error);
      return result;
    });
  } catch (error) {
    if (error instanceof Rollback) return err(error.error);
    throw error;
  }
}
