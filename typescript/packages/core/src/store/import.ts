import { containsSecret } from "../redact";
import { knownEvents } from "../reduce";
import { verifyRequests } from "../render";
import { err, ok, type Result } from "../result";
import { type Segment, type VerifiedLog, verifyExport } from "../verify";
import { type LogError, logError } from "../verify/error";
import { projectApprovals } from "./approvals";
import type { ArtifactStore } from "./artifacts";
import type { Tx } from "./driver";
import { knownOf } from "./indexing";
import { projectQuestions } from "./questions";
import {
  eventLines,
  getBranch,
  insertBranch,
  insertEvents,
  threadOwner,
} from "./tables";

/**
 * An export verified for import: its chain, and every model request replayed from the log and
 * the artifacts already stored (C7, Render v1). Imported bytes are stored exactly as exported,
 * torn tail included, so one holding a registered value is refused (C5).
 */
export async function verifiedImport(
  bytes: Uint8Array,
  artifacts: ArtifactStore,
): Promise<Result<VerifiedLog, LogError>> {
  if (containsSecret(bytes))
    return err(
      logError(
        "secret_in_stored_bytes",
        "the export holds a registered secret; nothing imported",
      ),
    );
  const log = verifyExport(bytes);
  if (!log.ok) return log;
  const replayed = await verifyRequests(knownEvents(log.value), artifacts);
  return replayed.ok ? log : replayed;
}

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
export async function importSegments(
  tx: Tx,
  log: VerifiedLog,
  target: ImportTarget,
): Promise<Result<void, LogError>> {
  for (const index of log.segments.keys()) {
    const stored = await importSegment(tx, log, target, index);
    if (!stored.ok) return stored;
  }
  return ok(undefined);
}

/**
 * A new segment is inserted, unless another tenant owns its thread (`branch_exists`, the owner
 * unnamed). A stored one must hold the same lines and, for the leaf, the same head evidence.
 */
async function importSegment(
  tx: Tx,
  log: VerifiedLog,
  target: ImportTarget,
  index: number,
): Promise<Result<void, LogError>> {
  const segment = log.segments[index];
  if (segment === undefined) throw new Error("segment index in range");
  const leaf = index === log.segments.length - 1;
  const existing = await getBranch(tx, segment.header.branch_id);
  if (!existing.ok) return existing;
  const row = existing.value;
  if (row === undefined) {
    const thread = segment.header.thread_id;
    const owner = await threadOwner(tx, thread);
    if (!owner.ok) return owner;
    if (owner.value !== undefined && owner.value !== target.tenantId)
      return err(logError("branch_exists", `thread ${thread} already exists`));
    return await insertSegment(tx, log, target, index, leaf);
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
    ? await sameLines(tx, segment, row.header_line)
    : err(conflict(segment));
}

async function insertSegment(
  tx: Tx,
  log: VerifiedLog,
  target: ImportTarget,
  index: number,
  leaf: boolean,
): Promise<Result<void, LogError>> {
  const segment = log.segments[index];
  if (segment === undefined) throw new Error("segment index in range");
  const fork = index === 0 ? undefined : segment.events[0]?.event;
  const inspect = leaf && (log.fold.repair || !log.headVerified);
  const inserted = await insertBranch(tx, {
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
  if (!inserted)
    return err(
      logError(
        "branch_exists",
        `thread ${segment.header.thread_id} already exists`,
      ),
    );
  await insertEvents(tx, segment.header.branch_id, segment.events);
  // The historical projector: rows from the segment's settlement events, never today's clock,
  // so a grant made before its challenge expired imports as granted.
  const { thread_id, branch_id } = segment.header;
  const events = knownOf(segment.events);
  await projectApprovals(
    tx,
    { tenant_id: target.tenantId, thread_id, branch_id },
    events,
  );
  await projectQuestions(
    tx,
    { tenant: target.tenantId, branch: branch_id },
    events,
    (e) => e.time,
  );
  return ok(undefined);
}

async function sameLines(
  tx: Tx,
  segment: Segment,
  headerLine: Uint8Array,
): Promise<Result<void, LogError>> {
  const through = segment.events.at(-1)?.event.seq ?? 0;
  const lines = await eventLines(tx, segment.header.branch_id, through);
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
