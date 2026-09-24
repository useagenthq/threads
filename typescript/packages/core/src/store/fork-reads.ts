// Read-side queries for forks: fork eligibility, a branch's chain through its parents, and the
// listed and mid-fork branches. The fork writes are in fork-writes.ts.

import { z } from "zod";
import type { Fold } from "../fold/state";
import { BranchId } from "../log";
import type { Strict } from "../log/zod-types";
import { err, ok, type Result } from "../result";
import { addLine, type Chain, emptyChain } from "../verify";
import { type LogError, logError } from "../verify/error";
import type { SqliteDriver } from "./driver";
import { branchLines } from "./lines";
import { parseRows } from "./tables";

const Id: Strict<{ branch_id: typeof BranchId }> = z.strictObject({
  branch_id: BranchId,
});

/** This tenant's branches a fork left `forking`: a crash stopped them before step 4. */
export function forkingBranches(
  db: SqliteDriver,
  tenantId: string,
): Result<readonly BranchId[], LogError> {
  const rows = parseRows(
    Id,
    db.all(
      "SELECT branch_id FROM branches WHERE tenant_id = ? AND state = 'forking' ORDER BY rowid",
      [tenantId],
    ),
  );
  return rows.ok ? ok(rows.value.map((r) => r.branch_id)) : rows;
}

/** A stored branch's chain, every line admitted as on import; a forking branch has no head line. */
export function loadChain(
  db: SqliteDriver,
  branchId: string,
): Result<Chain, LogError> {
  const lines = branchLines(db, branchId);
  if (!lines.ok) return lines;
  const chain = emptyChain();
  for (const line of lines.value.lines) {
    const added = addLine(chain, line);
    if (!added.ok) return added;
  }
  return ok(chain);
}

/**
 * a fork point is a snapshot where the quiescence predicate held and that has
 * not expired at `now`. Errors carry the requested seq.
 */
export function forkEligible(
  fold: Fold,
  atSeq: number,
  now: number,
): Result<void, LogError> {
  const snapshot = fold.snapshots.find((s) => s.seq === atSeq);
  if (snapshot === undefined || !snapshot.quiescent)
    return err(
      logError(
        "no_snapshot_boundary",
        `seq ${atSeq} is not a quiescent snapshot`,
        atSeq,
      ),
    );
  return snapshot.expiresAt !== null && snapshot.expiresAt <= now
    ? err(
        logError(
          "snapshot_expired",
          `the snapshot at seq ${atSeq} has expired`,
          atSeq,
        ),
      )
    : ok(undefined);
}

const Listed: Strict<{
  branch_id: typeof BranchId;
  parent_branch_id: z.ZodNullable<typeof BranchId>;
  fork_at_seq: z.ZodNullable<z.ZodInt>;
  state: z.ZodEnum<{ ready: "ready"; inspection_only: "inspection_only" }>;
}> = z.strictObject({
  branch_id: BranchId,
  parent_branch_id: BranchId.nullable(),
  fork_at_seq: z.int().nullable(),
  state: z.enum(["ready", "inspection_only"]),
});
export type ListedBranch = z.infer<typeof Listed>;

/** A thread's listed branches, oldest first: ready or inspection-only, never forking or failed. */
export function listedBranches(
  db: SqliteDriver,
  threadId: string,
  tenantId: string,
): Result<readonly ListedBranch[], LogError> {
  return parseRows(
    Listed,
    db.all(
      `SELECT branch_id, parent_branch_id, fork_at_seq, state FROM branches
        WHERE thread_id = ? AND tenant_id = ? AND state IN ('ready', 'inspection_only')
        ORDER BY rowid`,
      [threadId, tenantId],
    ),
  );
}
