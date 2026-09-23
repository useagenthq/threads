import type { Fold } from "../fold/state";
import { err, ok, type Result } from "../result";
import { type LogError, logError } from "../verify/error";

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
