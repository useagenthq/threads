import type { BranchId, ThreadId } from "../log";
import { err, ok, type Result } from "../result";
import { addLine, type Chain, emptyChain } from "../verify";
import { type LogError, logError } from "../verify/error";
import { admitDrafts, type EventDraft } from "./admit";
import { newBranch } from "./branch";
import type { Sql, Tx } from "./driver";
import { indexAppend, knownOf } from "./indexing";
import {
  atomically,
  getBranch,
  insertEvents,
  putLease,
  threadOwner,
} from "./tables";

/** A new root branch, the lease that holds it first, and its first events. */
export type BranchOpening = {
  readonly tenantId: string;
  readonly threadId: ThreadId;
  readonly branchId: BranchId;
  /** Taken at epoch 1; `ttlMs` 0 leaves it free for the next writer at once. */
  readonly lease: { readonly holderId: string; readonly ttlMs: number };
  readonly drafts: readonly EventDraft[];
};

/** A new branch opens a new thread: never a second root of one already stored. */
async function newThread(
  tx: Tx,
  threadId: ThreadId,
): Promise<Result<void, LogError>> {
  const owner = await threadOwner(tx, threadId);
  if (!owner.ok) return owner;
  return owner.value === undefined
    ? ok(undefined)
    : err(logError("invalid_transition", `thread ${threadId} already exists`));
}

/** The branch already exists: whoever opened it did this open's work, so the caller is done. */
export const ALREADY_OPEN = "already_open";

/**
 * `branch.open`: in one transaction (a savepoint inside another writer's), inserts the thread
 * and branch rows (a thread already stored is refused), admits the drafts as a writer would, stores them, takes the first lease at
 * epoch 1 and runs the index hooks. A new branch is one nobody can hold yet, so this never
 * appends to a live branch. Returns the new chain, or `already_open`.
 */
export async function openBranch(
  sql: Sql,
  now: number,
  opening: BranchOpening,
): Promise<Result<Chain | typeof ALREADY_OPEN, LogError>> {
  const { tenantId, threadId, branchId, lease, drafts } = opening;
  return await atomically<Chain | typeof ALREADY_OPEN>(sql, async (tx) => {
    const existing = await getBranch(tx, branchId);
    if (!existing.ok) return existing;
    if (existing.value !== undefined) return ok(ALREADY_OPEN);
    const fresh = await newThread(tx, threadId);
    if (!fresh.ok) return fresh;
    const made = await newBranch(tx, {
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
    await insertEvents(tx, branchId, events);
    await putLease(tx, branchId, {
      holder_id: lease.holderId,
      epoch: 1,
      expires_at: now + lease.ttlMs,
    });
    const indexed = await indexAppend({
      tx,
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
