import { ok, type Result } from "../result";
import type { VerifiedLog } from "../verify";
import type { LogError } from "../verify/error";
import type { ArtifactStore } from "./artifacts";
import type { SqliteDriver } from "./driver";
import type { BranchRow } from "./tables";
import type { Writer } from "./writer";

// wire rule 14: a torn JSONL import serves its valid prefix, keeps the dropped bytes as an
// artifact, and the branch records log_repaired when it is next acquired to run. The row is
// made runnable and the event appended in the acquiring transaction, so neither exists alone.

/** A leaf imported with a torn tail: unverified, its dropped bytes kept. */
export function isTorn(row: BranchRow): boolean {
  return (
    row.state === "inspection_only" &&
    row.head_verified === 0 &&
    row.dropped_ref !== null
  );
}

/** The served prefix becomes the verified head; the torn bytes stay as evidence. */
export function markRepaired(db: SqliteDriver, branchId: string): void {
  db.run(
    "UPDATE branches SET state = 'ready', head_verified = 1 WHERE branch_id = ?",
    [branchId],
  );
}

/** Appends log_repaired{truncated_bytes, at_offset, dropped_ref} under the new lease. */
export function recordRepair(
  writer: Writer,
  artifacts: ArtifactStore,
  row: BranchRow,
  log: VerifiedLog,
): Result<Writer, LogError> {
  const sha256 = row.dropped_ref;
  if (sha256 === null) throw new Error("a torn branch keeps its dropped bytes");
  const dropped = artifacts.get(sha256);
  if (!dropped.ok) return dropped;
  const appended = writer.append([
    {
      type: "log_repaired",
      type_version: 1,
      critical: false,
      actor: { kind: "host" },
      data: {
        truncated_bytes: dropped.value.length,
        at_offset: log.committedBytes,
        dropped_ref: {
          sha256,
          bytes: dropped.value.length,
          media_type: "application/octet-stream",
        },
      },
    },
  ]);
  return appended.ok ? ok(writer) : appended;
}
