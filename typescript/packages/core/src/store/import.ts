import { err, ok, type Result } from "../result";
import type { Segment, VerifiedLog } from "../verify";
import { type LogError, logError } from "../verify/error";
import type { SqliteDriver } from "./driver";
import {
  eventLines,
  getBranch,
  insertBranch,
  insertEvents,
  threadOwner,
} from "./tables";

/** Whose rows an import writes, and the artifact holding a torn tail's bytes, if any. */
export type ImportTarget = {
  readonly tenantId: string;
  readonly droppedRef: string | null;
};

/**
 * Stores a verified export's segments as branches, byte for byte. An ancestor segment is only
 * the parent's prefix through the fork point, so it is stored read-only. A branch already in
 * the store is referenced, if its stored lines start with the segment's. A leaf without a
 * verified head keeps that evidence and stays inspection-only until recovery repairs it.
 */
// ponytail: an ancestor stored shorter than the segment is refused, not extended.
export function importSegments(
  db: SqliteDriver,
  log: VerifiedLog,
  target: ImportTarget,
): Result<void, LogError> {
  for (const index of log.segments.keys()) {
    const stored = importSegment(db, log, target, index);
    if (!stored.ok) return stored;
  }
  return ok(undefined);
}

/**
 * A new segment is inserted, unless another tenant owns its thread (`branch_exists`, the owner
 * unnamed). A stored one must hold the same lines and, for the leaf, the same head evidence.
 */
function importSegment(
  db: SqliteDriver,
  log: VerifiedLog,
  target: ImportTarget,
  index: number,
): Result<void, LogError> {
  const segment = log.segments[index];
  if (segment === undefined) throw new Error("segment index in range");
  const leaf = index === log.segments.length - 1;
  const existing = getBranch(db, segment.header.branch_id);
  if (!existing.ok) return existing;
  const row = existing.value;
  if (row === undefined) {
    const thread = segment.header.thread_id;
    const owner = threadOwner(db, thread);
    if (!owner.ok) return owner;
    if (owner.value !== undefined && owner.value !== target.tenantId)
      return err(logError("branch_exists", `thread ${thread} already exists`));
    return insertSegment(db, log, target, index, leaf);
  }
  if (row.tenant_id !== target.tenantId)
    return err(logError("branch_not_found", `no branch ${row.branch_id}`));
  const last = segment.events.at(-1)?.event.seq ?? 0;
  const sameEvidence =
    !leaf ||
    (row.head_seq === last &&
      row.head_verified === (log.headVerified ? 1 : 0) &&
      row.dropped_ref === target.droppedRef);
  return sameEvidence
    ? sameLines(db, segment, row.header_line)
    : err(conflict(segment));
}

function insertSegment(
  db: SqliteDriver,
  log: VerifiedLog,
  target: ImportTarget,
  index: number,
  leaf: boolean,
): Result<void, LogError> {
  const segment = log.segments[index];
  if (segment === undefined) throw new Error("segment index in range");
  const fork = index === 0 ? undefined : segment.events[0]?.event;
  const inspect = leaf && (log.fold.repair || !log.headVerified);
  insertBranch(db, {
    branch_id: segment.header.branch_id,
    thread_id: segment.header.thread_id,
    tenant_id: target.tenantId,
    parent_branch_id: log.segments[index - 1]?.header.branch_id ?? null,
    fork_at_seq: fork === undefined ? null : fork.seq - 1,
    header_line: segment.bytes,
    state: leaf ? (inspect ? "inspection_only" : "ready") : "read_only",
    head_seq: segment.events.at(-1)?.event.seq ?? 0,
    head_hash: segment.events.at(-1)?.hash ?? segment.hash,
    head_verified: leaf && !log.headVerified ? 0 : 1,
    dropped_ref: leaf ? target.droppedRef : null,
  });
  insertEvents(db, segment.header.branch_id, segment.events);
  return ok(undefined);
}

function sameLines(
  db: SqliteDriver,
  segment: Segment,
  headerLine: Uint8Array,
): Result<void, LogError> {
  const through = segment.events.at(-1)?.event.seq ?? 0;
  const lines = eventLines(db, segment.header.branch_id, through);
  if (!lines.ok) return lines;
  const same =
    equal(headerLine, segment.bytes) &&
    lines.value.length === segment.events.length &&
    lines.value.every((line, i) => equal(line, segment.events[i]?.bytes));
  return same ? ok(undefined) : err(conflict(segment));
}

function conflict(segment: Segment): LogError {
  const id = segment.header.branch_id;
  return logError("seq_conflict", `branch ${id} is stored with other lines`);
}

function equal(a: Uint8Array, b: Uint8Array | undefined): boolean {
  return (
    b !== undefined && a.length === b.length && a.every((x, i) => x === b[i])
  );
}
