import { type BranchId, ThreadId } from "../log";
import { contextPolicy } from "../loop/policy";
import {
  type CacheBreak,
  type Cost,
  cacheBreaks,
  cost,
  knownEvents,
  mergeCost,
  type ReducedState,
  reduce,
} from "../reduce";
import { err, ok, type Result } from "../result";
import type { LogStore } from "../store";
import { type LogError, logError, type VerifiedLog } from "../verify";
import { readLog } from "./read";

// Thread usage(), cost() and cacheBreaks() (spec/api.json): each reads the branch once and
// projects that verified log. A failed read is an error, never a zero.

export type ThreadUsage = {
  /** Usage over this branch's resolved chain (a fork includes its parent's prefix). */
  readonly usage: () => Promise<Result<ReducedState["usage"], LogError>>;
  /** null when the thread pins no currency or no models. */
  readonly cost: (options?: {
    readonly tree?: boolean;
  }) => Promise<Result<Cost | null, LogError>>;
  readonly cacheBreaks: () => Promise<Result<readonly CacheBreak[], LogError>>;
};

export function usageMethods(log: LogStore, branchId: BranchId): ThreadUsage {
  return {
    usage: async () => {
      const read = readLog(log, branchId);
      return read.ok ? ok(reduce(read.value, log.now()).usage) : read;
    },
    cost: async (options = {}) => {
      const read = readLog(log, branchId);
      if (!read.ok) return read;
      const total =
        options.tree === true
          ? treeCost(log, read.value)
          : ok(ownCost(read.value));
      return total.ok ? ok(total.value ?? null) : total;
    },
    cacheBreaks: async () => {
      const read = readLog(log, branchId);
      if (!read.ok) return read;
      // The effective ttl, so an agent that pinned no context still gets a list.
      const { cache_ttl_ms } = contextPolicy(read.value.fold.policy);
      return ok(cacheBreaks(knownEvents(read.value), cache_ttl_ms));
    },
  };
}

function ownCost(chain: VerifiedLog): Cost | undefined {
  return cost(knownEvents(chain), chain.fold.policy);
}

/** This thread's cost plus every descendant's, each read from its own main branch. */
function treeCost(
  log: LogStore,
  chain: VerifiedLog,
): Result<Cost | undefined, LogError> {
  let total = ownCost(chain);
  if (total === undefined) return ok(undefined);
  for (const child of chain.fold.children.keys()) {
    const branch = log.mainBranch(ThreadId.parse(child));
    // A child with no thread yet was never started: it spent nothing.
    if (!branch.ok && branch.error.code === "branch_not_found") continue;
    const read = branch.ok ? readLog(log, branch.value) : branch;
    const part = read.ok ? treeCost(log, read.value) : read;
    if (!part.ok) {
      const { code, message, seq } = part.error;
      return err(logError(code, `child ${child}: ${message}`, seq));
    }
    total = mergeCost(total, part.value);
  }
  return ok(total);
}
