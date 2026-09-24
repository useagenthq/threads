import type { BranchId, ThreadId } from "../log";
import { ok, type Result } from "../result";
import { addLine, type Chain, emptyChain } from "../verify";
import type { LogError } from "../verify/error";
import { admitDrafts, type EventDraft } from "./admit";
import { newBranch } from "./branch";
import type { SqliteDriver } from "./driver";
import { indexAppend, knownOf } from "./indexing";
import { atomically, getBranch, insertEvents, putLease } from "./tables";

/** A new root branch, the lease that holds it first, and its first events. */
export type BranchOpening = {
  readonly tenantId: string;
  readonly threadId: ThreadId;
  readonly branchId: BranchId;
  /** Taken at epoch 1; `ttlMs` 0 leaves it free for the next writer at once. */
  readonly lease: { readonly holderId: string; readonly ttlMs: number };
  readonly drafts: readonly EventDraft[];
};

/** The branch already exists: whoever opened it did this open's work, so the caller is done. */
export const ALREADY_OPEN = "already_open";

/**
 * `branch.open`: in one transaction (a savepoint inside another writer's), inserts the thread
 * and branch rows, admits the drafts as a writer would, stores them, takes the first lease at
 * epoch 1 and runs the index hooks. A new branch is one nobody can hold yet, so this never
 * appends to a live branch. Returns the new chain, or `already_open`.
 */
export function openBranch(
  db: SqliteDriver,
  now: number,
  opening: BranchOpening,
): Result<Chain | typeof ALREADY_OPEN, LogError> {
  const { tenantId, threadId, branchId, lease, drafts } = opening;
  return atomically<Chain | typeof ALREADY_OPEN>(db, () => {
    const existing = getBranch(db, branchId);
    if (!existing.ok) return existing;
    if (existing.value !== undefined) return ok(ALREADY_OPEN);
    const made = newBranch(db, {
      tenantId,
      threadId,
      branchId,
      parent: null,
      state: "ready",
      createdAt: now,
    });
    if (!made.ok) return made;
    const chain = emptyChain();
    const header = addLine(chain, made.value);
    if (!header.ok) return header;
    const admitted = admitDrafts(chain, drafts, now, 1);
    if (!admitted.ok) return admitted;
    const { events, opened } = admitted.value;
    insertEvents(db, branchId, events);
    putLease(db, branchId, {
      holder_id: lease.holderId,
      epoch: 1,
      expires_at: now + lease.ttlMs,
    });
    const indexed = indexAppend({
      db,
      tenant: tenantId,
      threadId,
      branchId,
      events: knownOf(events),
      opened,
      holderId: lease.holderId,
      now,
    });
    return indexed.ok ? ok(chain) : indexed;
  });
}
