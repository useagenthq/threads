import {
  BranchId,
  EventId,
  type LogError,
  parseRows,
  type Result,
  type SqliteDriver,
  ThreadId,
} from "@threads/core/host";
import { z } from "zod";

// store.sql run_receipts: POST /v1/runs idempotency. A key is unique per
// tenant and operation; its principal and body hash are the binding it is checked against.

export const START_RUN = "start_run";

export type Receipt = {
  readonly principal_key: string;
  readonly body_hash: string;
  readonly thread_id: ThreadId;
  readonly branch_id: BranchId;
  readonly run_id: EventId;
};

const Row: z.ZodType<Receipt> = z.strictObject({
  principal_key: z.string(),
  body_hash: z.string(),
  thread_id: ThreadId,
  branch_id: BranchId,
  run_id: EventId,
});

export type Keyed = {
  readonly tenant: string;
  readonly key: string;
};

export function findReceipt(
  db: SqliteDriver,
  at: Keyed,
): Result<Receipt | undefined, LogError> {
  const rows = parseRows(
    Row,
    db.all(
      `SELECT principal_key, body_hash, thread_id, branch_id, run_id FROM run_receipts
        WHERE tenant_id = ? AND operation = ? AND idempotency_key = ?`,
      [at.tenant, START_RUN, at.key],
    ),
  );
  return rows.ok ? { ok: true, value: rows.value[0] } : rows;
}

/**
 * Inserts the receipt inside the user_input's append transaction. When another request won the
 * key first, the insert does nothing and this answers false, which rolls that append back.
 */
export function insertReceipt(
  db: SqliteDriver,
  at: Keyed,
  receipt: Receipt,
  now: number,
): boolean {
  db.run(
    `INSERT INTO run_receipts (tenant_id, operation, idempotency_key, principal_key, body_hash,
      thread_id, branch_id, run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT DO NOTHING`,
    [
      at.tenant,
      START_RUN,
      at.key,
      receipt.principal_key,
      receipt.body_hash,
      receipt.thread_id,
      receipt.branch_id,
      receipt.run_id,
      now,
    ],
  );
  const stored = findReceipt(db, at);
  return stored.ok && stored.value?.run_id === receipt.run_id;
}

export type RunBranch = {
  readonly tenant_id: string;
  readonly thread_id: ThreadId;
  readonly branch_id: BranchId;
};

const RunBranchRow: z.ZodType<RunBranch> = z.strictObject({
  tenant_id: z.string(),
  thread_id: ThreadId,
  branch_id: BranchId,
});

/**
 * Every tenant's branches that API runs went to, except those whose last event is a
 * turn_completed: where a crash may have left a run's turn open. The fold decides; this only
 * skips branches that are certainly closed, so a restart doesn't read every API thread's log.
 * Storage is a boundary: a row that fails its schema is skipped and said, never the reason the
 * valid ones aren't recovered.
 */
export function unfinishedRuns(db: SqliteDriver): readonly RunBranch[] {
  const rows = db.all(
    `SELECT DISTINCT r.tenant_id, r.thread_id, r.branch_id FROM run_receipts r
      JOIN branches b ON b.branch_id = r.branch_id AND b.tenant_id = r.tenant_id
      JOIN events e ON e.branch_id = b.branch_id AND e.seq = b.head_seq
      WHERE e.type <> 'turn_completed'`,
    [],
  );
  return rows.flatMap((row) => {
    const parsed = RunBranchRow.safeParse(row);
    if (parsed.success) return [parsed.data];
    // Only the branch, bounded, and the fields that failed: the row's text is untrusted.
    const branch =
      typeof row === "object" && row !== null && "branch_id" in row
        ? String(row.branch_id).slice(0, 64)
        : "?";
    const bad = parsed.error.issues.map((i) => i.path.join(".")).join(", ");
    console.error(
      `threads store: run_receipts row skipped (branch ${JSON.stringify(branch)}; bad field ${bad})`,
    );
    return [];
  });
}
