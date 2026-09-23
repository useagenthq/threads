import { sha256Hex } from "../hash";
import type { BranchId, ThreadId } from "../log";
import { err, ok, type Result } from "../result";
import { type LogError, logError } from "../verify/error";
import { VERSION } from "../version";
import type { SqliteDriver } from "./driver";
import { canonicalLine } from "./encode";
import { type BranchRow, getBranch, insertBranch } from "./tables";
import { IMPL } from "./writer";

export type NewBranch = {
  readonly tenantId: string;
  readonly threadId: ThreadId;
  readonly branchId: BranchId;
  /** A child's parent and fork point; null for a root. */
  readonly parent: {
    readonly branchId: BranchId;
    readonly atSeq: number;
  } | null;
  readonly state: BranchRow["state"];
  readonly createdAt: number;
};

/** Stores a branch row with this implementation's header line; an existing id is refused. */
export function newBranch(
  db: SqliteDriver,
  branch: NewBranch,
): Result<void, LogError> {
  const existing = getBranch(db, branch.branchId);
  if (!existing.ok) return existing;
  if (existing.value !== undefined)
    return err(
      logError(
        "invalid_transition",
        `branch ${branch.branchId} already exists`,
      ),
    );
  const header = canonicalLine({
    format: "threads.log",
    format_version: 1,
    thread_id: branch.threadId,
    branch_id: branch.branchId,
    created_at: branch.createdAt,
    writer: { impl: IMPL, version: VERSION },
  });
  if (!header.ok) return header;
  insertBranch(db, {
    branch_id: branch.branchId,
    thread_id: branch.threadId,
    tenant_id: branch.tenantId,
    parent_branch_id: branch.parent?.branchId ?? null,
    fork_at_seq: branch.parent?.atSeq ?? null,
    header_line: header.value,
    state: branch.state,
    head_seq: branch.parent?.atSeq ?? 0,
    head_hash: sha256Hex(header.value),
    head_verified: 1,
    dropped_ref: null,
  });
  return ok(undefined);
}
