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
  /**
   * Token totals over this branch (a fork counts its parent's prefix). A response whose count
   * the provider didn't report is counted in unknown_responses, never as zero.
   */
  readonly usage: () => Promise<Result<ReducedState["usage"], LogError>>;
  /**
   * What the thread spent, in nano-units of its pinned currency (USD for agent()), with a
   * conservative upper bound. null when the thread pins no prices. `tree: true` adds every
   * descendant subagent; one that declares no price makes the total incomplete and unbounded.
   */
  readonly cost: (options?: {
    readonly tree?: boolean;
  }) => Promise<Result<Cost | null, LogError>>;
  /** Turns whose prompt-cache reads dropped sharply, each with its likely cause. */
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

/** This thread's cost plus the cost of every descendant at any depth. */
function treeCost(
  log: LogStore,
  chain: VerifiedLog,
): Result<Cost | undefined, LogError> {
  const parts = descendantCosts(log, chain);
  if (!parts.ok) return parts;
  const own = ownCost(chain);
  return ok(
    own === undefined
      ? undefined
      : parts.value.reduce<Cost>((total, part) => mergeCost(total, part), own),
  );
}

/**
 * Each descendant's own cost (undefined when unpriced), read from its main branch, depth
 * first. An unpriced descendant doesn't stop the walk, and any unreadable one is the error.
 */
function descendantCosts(
  log: LogStore,
  chain: VerifiedLog,
): Result<readonly (Cost | undefined)[], LogError> {
  const out: (Cost | undefined)[] = [];
  for (const child of chain.fold.children.keys()) {
    const branch = log.mainBranch(ThreadId.parse(child));
    // A child with no thread yet was never started: it spent nothing.
    if (!branch.ok && branch.error.code === "branch_not_found") continue;
    const read = branch.ok ? readLog(log, branch.value) : branch;
    if (!read.ok) return err(inChild(child, read.error));
    const below = descendantCosts(log, read.value);
    if (!below.ok) return err(inChild(child, below.error));
    out.push(ownCost(read.value), ...below.value);
  }
  return ok(out);
}

/** The error, keeping its code, with the path to the descendant that failed. */
function inChild(child: string, error: LogError): LogError {
  return logError(error.code, `child ${child}: ${error.message}`, error.seq);
}
