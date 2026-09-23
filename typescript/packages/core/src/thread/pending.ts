import type { z } from "zod";
import type { Fold } from "../fold/state";
import type {
  BranchId,
  CallId,
  JsonObject,
  KnownEvent,
  PermissionRule,
} from "../log";
import type { ListedBranch } from "../store/forking";

// Thread.pendingApprovals and Thread.branches: projections of the log and the branch rows.

/** host-api PendingApproval. */
export type PendingApproval = {
  readonly challenge_id: string;
  readonly call_id: CallId;
  readonly tool: string;
  readonly input: z.infer<typeof JsonObject>;
  readonly args_hash: string;
  readonly expires_at: number;
  readonly suggested_rules: readonly z.infer<typeof PermissionRule>[];
};

/** host-api BranchInfo. */
export type BranchInfo = {
  readonly branch_id: BranchId;
  readonly parent_branch_id?: BranchId;
  readonly fork_at_seq?: number;
  readonly mode: "live" | "stub";
  readonly runnable: boolean;
};

/** Open challenges that have not expired, oldest first. */
export function pendingApprovals(
  events: readonly KnownEvent[],
  fold: Fold,
  now: number,
): readonly PendingApproval[] {
  return events.flatMap((e): PendingApproval[] => {
    if (e.type !== "approval_requested" || e.data.expires_at <= now) return [];
    if (fold.approvals.get(e.data.challenge_id)?.consumed !== false) return [];
    const call = events.find(
      (c) => c.type === "tool_call" && c.data.call_id === e.data.call_id,
    );
    if (call?.type !== "tool_call") return [];
    return [
      {
        challenge_id: e.data.challenge_id,
        call_id: call.data.call_id,
        tool: call.data.name,
        input: call.data.input,
        args_hash: e.data.args_hash,
        expires_at: e.data.expires_at,
        // ponytail: no rule suggestions until lands in the permission engine.
        suggested_rules: [],
      },
    ];
  });
}

// ponytail: every branch reports mode live; a stub fork records no mode yet.
export function branchInfo(row: ListedBranch): BranchInfo {
  return {
    branch_id: row.branch_id,
    ...(row.parent_branch_id === null
      ? {}
      : { parent_branch_id: row.parent_branch_id }),
    ...(row.fork_at_seq === null ? {} : { fork_at_seq: row.fork_at_seq }),
    mode: "live",
    runnable: row.state === "ready",
  };
}
