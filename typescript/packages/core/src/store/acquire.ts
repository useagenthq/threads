import type { BranchId } from "../log";
import { err, type Result } from "../result";
import type { VerifiedLog } from "../verify";
import { type LogError, logError } from "../verify/error";
import type { ArtifactStore } from "./artifacts";
import { READ_ONLY, type Tx } from "./driver";
import { type StoreAccess, takeLease } from "./lease";
import { isTorn, markRepaired, recordRepair } from "./repair";
import { atomically, type BranchRow, ownedBranch } from "./tables";
import { type Writer, writerMismatch } from "./writer";

// LogStore.acquire: the branch lease, and a torn import's repair recorded under it.

/**
 * Takes the branch lease: free or expired, else `branch_busy`. The new epoch is one above both
 * the old lease and every epoch on the resolved chain (wire rule 11). A branch imported with a
 * torn tail records log_repaired under the new lease and becomes runnable.
 */
export async function acquire(
  s: StoreAccess,
  artifacts: ArtifactStore,
  branchId: BranchId,
  holderId: string,
  ttlMs: number,
): Promise<Result<Writer, LogError>> {
  const dropped = await droppedBytes(s, artifacts, branchId);
  return atomically(s.db, async (tx) => {
    const row = await ownedBranch(tx, branchId, s.tenant);
    const torn = row.ok && isTorn(row.value) ? row.value : undefined;
    if (torn !== undefined) await markRepaired(tx, branchId);
    const log = await runnable(s, tx, branchId);
    if (!log.ok) return log;
    const writer = await takeLease(s, tx, branchId, holderId, ttlMs, log.value);
    if (!writer.ok || torn === undefined) return writer;
    const bytes =
      torn.dropped_ref === dropped?.sha256
        ? dropped.bytes
        : err(logError("artifact_missing", "the torn bytes changed"));
    return recordRepair(tx, writer.value, torn, log.value, bytes);
  });
}

/** A torn branch's dropped bytes, read before the acquiring transaction. */
async function droppedBytes(
  s: StoreAccess,
  artifacts: ArtifactStore,
  branchId: BranchId,
): Promise<
  | {
      readonly sha256: string;
      readonly bytes: Result<Uint8Array, LogError>;
    }
  | undefined
> {
  const row = await s.db.transaction(
    (tx) => ownedBranch(tx, branchId, s.tenant),
    READ_ONLY,
  );
  if (!row.ok || !isTorn(row.value) || row.value.dropped_ref === null)
    return undefined;
  const sha256 = row.value.dropped_ref;
  return { sha256, bytes: await artifacts.get(sha256) };
}

/**
 * The chain of a branch this implementation may write: this tenant's, `ready`, verified, and
 * headed by this implementation at this major version.
 */
async function runnable(
  s: StoreAccess,
  tx: Tx,
  branchId: BranchId,
): Promise<Result<VerifiedLog, LogError>> {
  const branch = await ownedBranch(tx, branchId, s.tenant);
  if (!branch.ok) return branch;
  const state: BranchRow["state"] = branch.value.state;
  if (state !== "ready")
    return err(
      logError(
        "branch_not_runnable",
        `branch ${branchId} is ${state}`,
        branch.value.head_seq,
      ),
    );
  const log = await s.read(tx, branchId);
  if (!log.ok)
    return err(logError("log_corrupt", log.error.message, log.error.seq));
  const mismatch = writerMismatch(log.value);
  return mismatch === undefined ? log : err(mismatch);
}
