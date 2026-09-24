import type { BranchId, SandboxId } from "../log";
import { err, ok, type Result } from "../result";
import { type Chain, tipHash, type VerifiedLog } from "../verify";
import { type LogError, logError } from "../verify/error";
import { newBranch } from "./branch";
import * as forking from "./forking";
import { grantLease, type StoreAccess, takeLease } from "./lease";
import { atomically, ownedBranch, putLease, setBranchState } from "./tables";
import type { Writer } from "./writer";

// The fork steps of LogStore (beginFork, reclaimFork, finishFork): each runs in one transaction.

export type ForkRequest = {
  readonly parent: BranchId;
  readonly atSeq: number;
  readonly branch: BranchId;
  readonly holderId: string;
};

/** Steps 1 and 3: the child's `forking` row with its own header, and its lease. */
export function beginFork(
  s: StoreAccess,
  request: ForkRequest,
  ttlMs: number,
): Result<Writer, LogError> {
  return atomically(s.db, () => {
    const owned = ownedBranch(s.db, request.parent, s.tenant);
    if (!owned.ok) return owned;
    const parent = s.read(request.parent);
    if (!parent.ok) return parent;
    const eligible = forking.forkEligible(
      parent.value.fold,
      request.atSeq,
      s.now(),
    );
    if (!eligible.ok) return eligible;
    const opened = openChild(s, parent.value, request);
    if (!opened.ok) return opened;
    return ok(
      grantLease(
        s,
        request.branch,
        request.holderId,
        opened.value.fold.epoch + 1,
        ttlMs,
        opened.value,
      ),
    );
  });
}

/** Retakes a `forking` branch's lease, like acquire, so its creator can finish or fail it. */
export function reclaimFork(
  s: StoreAccess,
  branchId: BranchId,
  holderId: string,
  ttlMs: number,
): Result<Writer, LogError> {
  return atomically(s.db, () => {
    const row = ownedBranch(s.db, branchId, s.tenant);
    if (!row.ok) return row;
    if (row.value.state !== "forking")
      return err(
        logError("branch_not_runnable", `branch ${branchId} is not forking`),
      );
    const chain = forking.loadChain(s.db, branchId);
    if (!chain.ok) return chain;
    return takeLease(s, branchId, holderId, ttlMs, chain.value);
  });
}

/** Step 4, in one transaction: the child's fork event bound to the parent's line, then ready. */
export function finishFork(
  s: StoreAccess,
  writer: Writer,
  restored: {
    readonly sandboxId: SandboxId;
    readonly knowledgePolicy: "pinned" | "current";
  },
): Result<void, LogError> {
  const parent = writer.chain.segments.at(-2)?.header.branch_id;
  if (parent === undefined) throw new Error("a forking chain has a parent");
  return atomically(s.db, () => {
    const forked = writer.append([
      {
        type: "fork",
        type_version: 1,
        critical: true,
        actor: { kind: "host" },
        data: {
          parent_branch_id: parent,
          at_hash: tipHash(writer.chain) ?? "",
          reason: "snapshot",
          sandbox_id: restored.sandboxId,
          knowledge_policy: restored.knowledgePolicy,
        },
      },
    ]);
    if (!forked.ok) return forked;
    setBranchState(s.db, writer.lease.branchId, "ready");
    // The fork is done with the child; hand the lease back so a run can take it at once.
    putLease(s.db, writer.lease.branchId, {
      holder_id: writer.lease.holderId,
      epoch: writer.lease.epoch,
      expires_at: s.now(),
    });
    return ok(undefined);
  });
}

/** Stores the child's row and header, and loads its chain: the parent's through at_seq. */
function openChild(
  s: StoreAccess,
  parent: VerifiedLog,
  request: ForkRequest,
): Result<Chain, LogError> {
  const threadId = parent.segments[0]?.header.thread_id;
  if (threadId === undefined) throw new Error("a verified log has a header");
  const stored = newBranch(s.db, {
    tenantId: s.tenant,
    threadId,
    branchId: request.branch,
    parent: { branchId: request.parent, atSeq: request.atSeq },
    state: "forking",
    createdAt: s.now(),
  });
  return stored.ok ? forking.loadChain(s.db, request.branch) : stored;
}
