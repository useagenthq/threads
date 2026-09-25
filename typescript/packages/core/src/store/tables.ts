import { z } from "zod";
import { BranchId, Int, PosInt, Sha256, ThreadId } from "../log";
import type { EnumOf, Strict } from "../log/zod-types";
import { err, ok, type Result } from "../result";
import type { ChainEvent } from "../verify";
import { type LogError, logError } from "../verify/error";
import { type Sql, type Tx, writing } from "./driver";

/** The tenant of local use: the local operator's (spec/api.json). */
export const LOCAL_TENANT = "local";

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
  tenant_id: z.ZodString;
  parent_branch_id: z.ZodNullable<typeof BranchId>;
  fork_at_seq: z.ZodNullable<typeof Int>;
  header_line: typeof Bytes;
  state: EnumOf<typeof BRANCH_STATES>;
  head_seq: typeof Int;
  head_hash: typeof Sha256;
  head_verified: z.ZodUnion<readonly [z.ZodLiteral<0>, z.ZodLiteral<1>]>;
  dropped_ref: z.ZodNullable<typeof Sha256>;
}> = z.strictObject({
  branch_id: BranchId,
  thread_id: ThreadId,
  tenant_id: z.string(),
  parent_branch_id: BranchId.nullable(),
  fork_at_seq: Int.nullable(),
  header_line: Bytes,
  state: z.enum(BRANCH_STATES),
  head_seq: Int,
  head_hash: Sha256,
  head_verified: z.union([z.literal(0), z.literal(1)]),
  dropped_ref: Sha256.nullable(),
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
export function parseRows<T>(
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

export async function getBranch(
  tx: Tx,
  branchId: string,
): Promise<Result<BranchRow | undefined, LogError>> {
  const rows = parseRows(
    BranchRow,
    await tx.all(
      `SELECT branch_id, thread_id, tenant_id, parent_branch_id, fork_at_seq, header_line, state,
        head_seq, head_hash, head_verified, dropped_ref FROM branches WHERE branch_id = ?`,
      [branchId],
    ),
  );
  return rows.ok ? ok(rows.value[0]) : rows;
}

const TenantRow: Strict<{ tenant_id: z.ZodString }> = z.strictObject({
  tenant_id: z.string(),
});

const BranchIdRow: Strict<{ branch_id: typeof BranchId }> = z.strictObject({
  branch_id: BranchId,
});

/** A thread's main branch: its root, the one without a parent. */
export async function rootBranch(
  tx: Tx,
  threadId: string,
  tenantId: string,
): Promise<Result<BranchId | undefined, LogError>> {
  const rows = parseRows(
    BranchIdRow,
    await tx.all(
      "SELECT branch_id FROM branches WHERE thread_id = ? AND tenant_id = ? AND parent_branch_id IS NULL ORDER BY rowid LIMIT 1",
      [threadId, tenantId],
    ),
  );
  return rows.ok ? ok(rows.value[0]?.branch_id) : rows;
}

/** The tenant that owns a thread, if the thread is stored. */
export async function threadOwner(
  tx: Tx,
  threadId: string,
): Promise<Result<string | undefined, LogError>> {
  const rows = parseRows(
    TenantRow,
    await tx.all("SELECT tenant_id FROM threads WHERE thread_id = ?", [
      threadId,
    ]),
  );
  return rows.ok ? ok(rows.value[0]?.tenant_id) : rows;
}

/** The branch, if it exists and belongs to `tenantId`; any other is `branch_not_found`. */
export async function ownedBranch(
  tx: Tx,
  branchId: string,
  tenantId: string,
): Promise<Result<BranchRow, LogError>> {
  const row = await getBranch(tx, branchId);
  if (!row.ok) return row;
  return row.value?.tenant_id === tenantId
    ? ok(row.value)
    : err(logError("branch_not_found", `no branch ${branchId}`));
}

/**
 * Inserts the branch and, for a new thread, its thread row. The foreign key refuses a branch
 * whose tenant is not its thread's. False when the branch is a root of a thread that has one
 * (store.sql branches_root): nothing was inserted.
 */
export async function insertBranch(tx: Tx, row: BranchRow): Promise<boolean> {
  await tx.run(
    "INSERT INTO threads (thread_id, tenant_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
    [row.thread_id, row.tenant_id],
  );
  const inserted = await tx.run(
    `INSERT INTO branches (branch_id, thread_id, tenant_id, parent_branch_id, fork_at_seq,
      header_line, state, head_seq, head_hash, head_verified, dropped_ref)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT (thread_id) WHERE parent_branch_id IS NULL DO NOTHING`,
    [
      row.branch_id,
      row.thread_id,
      row.tenant_id,
      row.parent_branch_id,
      row.fork_at_seq,
      row.header_line,
      row.state,
      row.head_seq,
      row.head_hash,
      row.head_verified,
      row.dropped_ref,
    ],
  );
  return inserted === 1;
}

export async function setBranchState(
  tx: Tx,
  branchId: string,
  state: BranchRow["state"],
): Promise<void> {
  await tx.run("UPDATE branches SET state = ? WHERE branch_id = ?", [
    state,
    branchId,
  ]);
}

/** Inserts a branch's own event rows and advances its head checkpoint. */
export async function insertEvents(
  tx: Tx,
  branchId: string,
  events: readonly ChainEvent[],
): Promise<void> {
  for (const { event, bytes } of events) {
    await tx.run(
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
  await tx.run(
    "UPDATE branches SET head_seq = ?, head_hash = ? WHERE branch_id = ?",
    [last.event.seq, last.hash, branchId],
  );
}

/** A branch's own event lines with seq ≤ `through`, in seq order. */
export async function eventLines(
  tx: Tx,
  branchId: string,
  through: number,
): Promise<Result<readonly Uint8Array[], LogError>> {
  const rows = parseRows(
    LineRow,
    await tx.all(
      "SELECT line FROM events WHERE branch_id = ? AND seq <= ? ORDER BY seq",
      [branchId, through],
    ),
  );
  return rows.ok ? ok(rows.value.map((row) => row.line)) : rows;
}

export async function getLease(
  tx: Tx,
  branchId: string,
): Promise<Result<LeaseRow | undefined, LogError>> {
  const rows = parseRows(
    LeaseRow,
    await tx.all(
      "SELECT holder_id, epoch, expires_at FROM leases WHERE branch_id = ?",
      [branchId],
    ),
  );
  return rows.ok ? ok(rows.value[0]) : rows;
}

export async function putLease(
  tx: Tx,
  branchId: string,
  lease: LeaseRow,
): Promise<void> {
  await tx.run(
    `INSERT INTO leases (branch_id, holder_id, epoch, expires_at) VALUES (?, ?, ?, ?)
      ON CONFLICT (branch_id) DO UPDATE SET holder_id = excluded.holder_id,
      epoch = excluded.epoch, expires_at = excluded.expires_at`,
    [branchId, lease.holder_id, lease.epoch, lease.expires_at],
  );
}

class Rollback extends Error {
  readonly error: LogError;

  constructor(error: LogError) {
    super(error.message);
    this.error = error;
  }
}

/**
 * Runs `fn` in one transaction (a savepoint inside an open one) and rolls it back when `fn`
 * returns an error value. `fn` may run again from the start (see `Tx`).
 */
export async function atomically<T>(
  sql: Sql,
  fn: (tx: Tx) => Promise<Result<T, LogError>>,
): Promise<Result<T, LogError>> {
  try {
    return await writing(sql, async (tx) => {
      const result = await fn(tx);
      if (!result.ok) throw new Rollback(result.error);
      return result;
    });
  } catch (error) {
    if (error instanceof Rollback) return err(error.error);
    throw error;
  }
}
