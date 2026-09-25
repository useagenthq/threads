import { err, ok, type Result } from "../result";
import type { LogError } from "../verify/error";
import { logError } from "../verify/error";
import type { ArtifactStore } from "./artifacts";
import type { Tx } from "./driver";
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
export async function branchLines(
  tx: Tx,
  branchId: string,
): Promise<Result<BranchLines, LogError>> {
  const segments: Uint8Array[][] = [];
  let leaf: BranchRow | undefined;
  let id: string | null = branchId;
  let through = Number.MAX_SAFE_INTEGER;
  while (id !== null) {
    const row = await getBranch(tx, id);
    if (!row.ok) return row;
    if (row.value === undefined)
      return err(logError("branch_not_found", `no branch ${id}`));
    leaf ??= row.value;
    // Only through the head read with the row: a line appended since (another process's) is
    // not in this read, so the head checkpoint stays the last line.
    const own = await eventLines(tx, id, Math.min(through, row.value.head_seq));
    if (!own.ok) return own;
    segments.unshift([row.value.header_line, ...own.value]);
    through = Math.min(through, row.value.fork_at_seq ?? 0);
    id = row.value.parent_branch_id;
  }
  if (leaf === undefined) return err(logError("branch_not_found", "no branch"));
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

/**
 * The branch's export: its lines, one per line with "\n", then its head line. A branch imported
 * without a verified head ends as it was imported: with its torn bytes, or with nothing.
 */
export async function exportBytes(
  tx: Tx,
  artifacts: ArtifactStore,
  branchId: string,
): Promise<Result<Uint8Array, LogError>> {
  const branch = await branchLines(tx, branchId);
  if (!branch.ok) return branch;
  const tail = await endLine(artifacts, branch.value.row);
  if (!tail.ok) return tail;
  const parts = [...branch.value.lines, tail.value];
  const size = parts.reduce((n, part) => n + part.length + 1, 0);
  const out = new Uint8Array(size - (branch.value.row.head_verified ? 0 : 1));
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    if (at + part.length < out.length) out[at + part.length] = 0x0a;
    at += part.length + 1;
  }
  return ok(out);
}

// Only a torn import reads an artifact here, inside the reading transaction.
async function endLine(
  artifacts: ArtifactStore,
  row: BranchRow,
): Promise<Result<Uint8Array, LogError>> {
  if (row.head_verified) return headLine(row);
  if (row.dropped_ref === null) return ok(new Uint8Array());
  return artifacts.get(row.dropped_ref);
}
