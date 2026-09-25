import type { BranchId, SandboxId } from "../log";
import { err, ok, type Result } from "../result";
import { type Chain, tipHash, type VerifiedLog } from "../verify";
import { type LogError, logError } from "../verify/error";
import { newBranch } from "./branch";
import type { Tx } from "./driver";
import * as forking from "./fork-reads";
import { grantLease, type StoreAccess, takeLease } from "./lease";
import { atomically, ownedBranch, putLease, setBranchState } from "./tables";
import type { Writer } from "./writer";

// The fork writes of LogStore (beginFork, reclaimFork, finishFork), each in one transaction.
// The queries they and LogStore read with are in fork-reads.ts.

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
): Promise<Result<Writer, LogError>> {
  return atomically(s.db, async (tx) => {
    const owned = await ownedBranch(tx, request.parent, s.tenant);
    if (!owned.ok) return owned;
    const parent = await s.read(tx, request.parent);
    if (!parent.ok) return parent;
    const eligible = forking.forkEligible(
      parent.value.fold,
      request.atSeq,
      s.now(),
    );
    if (!eligible.ok) return eligible;
    const opened = await openChild(s, tx, parent.value, request);
    if (!opened.ok) return opened;
    return ok(
      await grantLease(
        s,
        tx,
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
): Promise<Result<Writer, LogError>> {
  return atomically(s.db, async (tx) => {
    const row = await ownedBranch(tx, branchId, s.tenant);
    if (!row.ok) return row;
    if (row.value.state !== "forking")
      return err(
        logError("branch_not_runnable", `branch ${branchId} is not forking`),
      );
    const chain = await forking.loadChain(tx, branchId);
    if (!chain.ok) return chain;
    return takeLease(s, tx, branchId, holderId, ttlMs, chain.value);
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
): Promise<Result<void, LogError>> {
  const parent = writer.chain.segments.at(-2)?.header.branch_id;
  if (parent === undefined) throw new Error("a forking chain has a parent");
  return atomically(s.db, async (tx) => {
    const forked = await writer.appendIn(tx, [
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
    await setBranchState(tx, writer.lease.branchId, "ready");
    // The fork is done with the child; hand the lease back so a run can take it at once.
    await putLease(tx, writer.lease.branchId, {
      holder_id: writer.lease.holderId,
      epoch: writer.lease.epoch,
      expires_at: s.now(),
    });
    return ok(undefined);
  });
}

/** Stores the child's row and header, and loads its chain: the parent's through at_seq. */
async function openChild(
  s: StoreAccess,
  tx: Tx,
  parent: VerifiedLog,
  request: ForkRequest,
): Promise<Result<Chain, LogError>> {
  const threadId = parent.segments[0]?.header.thread_id;
  if (threadId === undefined) throw new Error("a verified log has a header");
  const stored = await newBranch(tx, {
    tenantId: s.tenant,
    threadId,
    branchId: request.branch,
    parent: { branchId: request.parent, atSeq: request.atSeq },
    state: "forking",
    createdAt: s.now(),
  });
  return stored.ok ? forking.loadChain(tx, request.branch) : stored;
}
