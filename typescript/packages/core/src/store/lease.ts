import type { BranchId } from "../log";
import { err, ok, type Result } from "../result";
import type { Chain, VerifiedLog } from "../verify";
import { type LogError, logError } from "../verify/error";
import type { SqliteDriver } from "./driver";
import { getLease, putLease } from "./tables";
import { Writer } from "./writer";

/** 30 s lease TTL, renewed every 10 s by the holder. */
export const LEASE_TTL_MS = 30_000;

/** What the lease and fork writes use of a LogStore: one tenant's connection, clock and reads. */
export type StoreAccess = {
  readonly db: SqliteDriver;
  readonly now: () => number;
  readonly tenant: string;
  readonly read: (branchId: BranchId) => Result<VerifiedLog, LogError>;
};

/** The lease if it is free, expired or already this holder's, at the next epoch (rule 11). */
export function takeLease(
  s: StoreAccess,
  branchId: BranchId,
  holderId: string,
  ttlMs: number,
  chain: Chain,
): Result<Writer, LogError> {
  const lease = getLease(s.db, branchId);
  if (!lease.ok) return lease;
  const held = lease.value;
  if (
    held !== undefined &&
    held.expires_at > s.now() &&
    held.holder_id !== holderId
  )
    return err(logError("branch_busy", `branch ${branchId} has a live lease`));
  const epoch = Math.max(held?.epoch ?? 0, chain.fold.epoch) + 1;
  return ok(grantLease(s, branchId, holderId, epoch, ttlMs, chain));
}

/** Writes the lease row and returns the writer that holds it. */
export function grantLease(
  s: StoreAccess,
  branchId: string,
  holderId: string,
  epoch: number,
  ttlMs: number,
  chain: Chain,
): Writer {
  putLease(s.db, branchId, {
    holder_id: holderId,
    epoch,
    expires_at: s.now() + ttlMs,
  });
  return new Writer(s.db, s.now, { branchId, holderId, epoch, ttlMs }, chain);
}
