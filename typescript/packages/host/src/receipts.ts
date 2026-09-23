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
