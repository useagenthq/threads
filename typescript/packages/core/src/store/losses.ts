import { z } from "zod";
import { Int, ThreadId } from "../log";
import type { Strict } from "../log/zod-types";
import type { Result } from "../result";
import type { LogError } from "../verify/error";
import { reading, type Sql, type Tx, writing } from "./driver";
import { parseRows } from "./tables";

// Loss accounting for telemetry exporters (store.sql observers and observer_losses): what a
// deletion may have dropped before an exporter sent it. Bookkeeping only; the log never reads it.

/**
 * In the delete transaction, before the thread's events go: one row per registered observer,
 * counting the thread's events past that observer's cursor on each branch. Never waits on a
 * collector; with no observer registered it inserts nothing.
 */
export async function recordLosses(
  tx: Tx,
  tenantId: string,
  threadId: string,
  now: number,
): Promise<void> {
  await tx.run(
    `INSERT INTO observer_losses
       (observer, tenant_id, thread_id, unchecked_events, deleted_at, reported_at)
     SELECT o.name, ?, ?, (
       SELECT count(*) FROM events e
         JOIN branches b ON b.branch_id = e.branch_id
         LEFT JOIN observer_cursors c ON c.observer = o.name AND c.branch_id = e.branch_id
         WHERE b.thread_id = ? AND e.seq > coalesce(c.seq, 0)
     ), ?, NULL
     FROM observers o WHERE true
     ON CONFLICT DO NOTHING`,
    [tenantId, threadId, threadId, now],
  );
}

/** Registers `observer` once, so deletions from now on record what it may not have sent. */
export async function registerObserver(
  sql: Sql,
  observer: string,
  now: number,
): Promise<void> {
  await writing(sql, (tx) =>
    tx.run(
      "INSERT INTO observers (name, registered_at) VALUES (?, ?) ON CONFLICT DO NOTHING",
      [observer, now],
    ),
  );
}

export const LossRow: Strict<{
  tenant_id: z.ZodString;
  thread_id: typeof ThreadId;
  unchecked_events: typeof Int;
  deleted_at: typeof Int;
}> = z.strictObject({
  tenant_id: z.string(),
  thread_id: ThreadId,
  unchecked_events: Int,
  deleted_at: Int,
});
export type LossRow = z.infer<typeof LossRow>;

/** The observer's loss rows not yet exported, oldest first. */
export async function unreportedLosses(
  sql: Sql,
  observer: string,
): Promise<Result<readonly LossRow[], LogError>> {
  return parseRows(
    LossRow,
    await reading(sql, (tx) =>
      tx.all(
        `SELECT tenant_id, thread_id, unchecked_events, deleted_at FROM observer_losses
          WHERE observer = ? AND reported_at IS NULL ORDER BY deleted_at, thread_id`,
        [observer],
      ),
    ),
  );
}

/** After the collector accepted their spans. */
export async function markReported(
  sql: Sql,
  observer: string,
  rows: readonly LossRow[],
  now: number,
): Promise<void> {
  await writing(sql, async (tx) => {
    for (const row of rows)
      await tx.run(
        `UPDATE observer_losses SET reported_at = ?
          WHERE observer = ? AND thread_id = ? AND deleted_at = ? AND reported_at IS NULL`,
        [now, observer, row.thread_id, row.deleted_at],
      );
  });
}
