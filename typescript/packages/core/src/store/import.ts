import { err, ok, type Result } from "../result";
import type { Segment, VerifiedLog } from "../verify";
import { type LogError, logError } from "../verify/error";
import type { SqliteDriver } from "./driver";
import { eventLines, getBranch, insertBranch, insertEvents } from "./tables";

/**
 * Stores a verified export's segments as branches, byte for byte. An ancestor segment is only
 * the parent's prefix through the fork point, so it is stored read-only. A branch already in
 * the store is referenced, if its stored lines start with the segment's.
 */
// ponytail: an ancestor stored shorter than the segment is refused, not extended.
export function importSegments(
  db: SqliteDriver,
  log: VerifiedLog,
): Result<void, LogError> {
  const leafAt = log.segments.length - 1;
  for (const [index, segment] of log.segments.entries()) {
    const existing = getBranch(db, segment.header.branch_id);
    if (!existing.ok) return existing;
    const stored =
      existing.value === undefined
        ? insertSegment(db, log, index, index === leafAt)
        : sameLines(db, segment, existing.value.header_line);
    if (!stored.ok) return stored;
  }
  return ok(undefined);
}

function insertSegment(
  db: SqliteDriver,
  log: VerifiedLog,
  index: number,
  leaf: boolean,
): Result<void, LogError> {
  const segment = log.segments[index];
  if (segment === undefined) throw new Error("segment index in range");
  const fork = index === 0 ? undefined : segment.events[0]?.event;
  const repair = leaf && log.fold.repair;
  insertBranch(db, {
    branch_id: segment.header.branch_id,
    thread_id: segment.header.thread_id,
    parent_branch_id: log.segments[index - 1]?.header.branch_id ?? null,
    fork_at_seq: fork === undefined ? null : fork.seq - 1,
    header_line: segment.bytes,
    state: leaf ? (repair ? "inspection_only" : "ready") : "read_only",
    head_seq: segment.events.at(-1)?.event.seq ?? 0,
    head_hash: segment.events.at(-1)?.hash ?? segment.hash,
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
  return same
    ? ok(undefined)
    : err(
        logError(
          "seq_conflict",
          `branch ${segment.header.branch_id} is stored with other lines`,
        ),
      );
}

function equal(a: Uint8Array, b: Uint8Array | undefined): boolean {
  return (
    b !== undefined && a.length === b.length && a.every((x, i) => x === b[i])
  );
}
