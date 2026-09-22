import { err, ok, type Result } from "../result";
import type { LogError } from "../verify/error";
import { logError } from "../verify/error";
import type { SqliteDriver } from "./driver";
import { canonicalLine } from "./encode";
import { type BranchRow, eventLines, getBranch } from "./tables";

/** A branch's stored lines in export order, and its row (for the head checkpoint). */
export type BranchLines = {
  readonly row: BranchRow;
  readonly lines: readonly Uint8Array[];
};

/**
 * Each ancestor segment through its fork point (header and lines, byte-identical), then the
 * branch's own header and lines (wire rule 12). No head line.
 */
export function branchLines(
  db: SqliteDriver,
  branchId: string,
): Result<BranchLines, LogError> {
  const segments: Uint8Array[][] = [];
  let leaf: BranchRow | undefined;
  let id: string | null = branchId;
  let through = Number.MAX_SAFE_INTEGER;
  while (id !== null) {
    const row = getBranch(db, id);
    if (!row.ok) return row;
    if (row.value === undefined)
      return err(logError("branch_not_runnable", `no branch ${id}`));
    leaf ??= row.value;
    const own = eventLines(db, id, through);
    if (!own.ok) return own;
    segments.unshift([row.value.header_line, ...own.value]);
    through = Math.min(through, row.value.fork_at_seq ?? 0);
    id = row.value.parent_branch_id;
  }
  if (leaf === undefined)
    return err(logError("branch_not_runnable", "no branch"));
  return ok({ row: leaf, lines: segments.flat() });
}

/** The Head checkpoint line for a branch row (wire rule 8). */
export function headLine(row: BranchRow): Result<Uint8Array, LogError> {
  return canonicalLine({
    format: "threads.head",
    format_version: 1,
    branch_id: row.branch_id,
    seq: row.head_seq,
    hash: row.head_hash,
  });
}

/** The branch's export: its lines, one per line with "\n", then its head line. */
export function exportBytes(
  db: SqliteDriver,
  branchId: string,
): Result<Uint8Array, LogError> {
  const branch = branchLines(db, branchId);
  if (!branch.ok) return branch;
  const head = headLine(branch.value.row);
  if (!head.ok) return head;
  const parts = [...branch.value.lines, head.value];
  const out = new Uint8Array(parts.reduce((n, part) => n + part.length + 1, 0));
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    out[at + part.length] = 0x0a;
    at += part.length + 1;
  }
  return ok(out);
}
