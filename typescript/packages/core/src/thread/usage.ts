import type { BranchId, CacheBreak, Cost, ThreadId, UsageTotals } from "../log";
import { contextPolicy } from "../loop/policy";
import { cacheBreaks, knownEvents, reduce } from "../reduce";
import { ok, type Result } from "../result";
import type { LogStore } from "../store";
import { type CostError, ownCost, treeCost } from "./cost-tree";
import { type ReadError, readLog } from "./read";

// Thread usage(), cost() and cacheBreaks() (spec/api.json): each reads the branch once and
// projects that verified log. A failed read is an error, never a zero.

export type ThreadUsage = {
  /**
   * Token totals over this branch (a fork counts its parent's prefix). A response whose count
   * the provider didn't report, or that would take a total past 2^53 - 1, is counted in
   * unknown_responses, never as zero.
   */
  readonly usage: () => Promise<Result<UsageTotals, ReadError>>;
  /**
   * What the thread spent, in nano-units of its pinned currency (USD for agent()), with a
   * conservative upper bound; null when the thread pins no prices. `tree: true` adds every
   * descendant subagent, in the root's currency (else the first priced descendant's; null when
   * none is priced). A thread that spent money it can't add (unpriced, or another currency)
   * makes the total incomplete and unbounded; one that made no model request changes nothing.
   * An amount past 2^53 - 1 nanos is cost_overflow, never a rounded one.
   */
  readonly cost: (options?: {
    readonly tree?: boolean;
  }) => Promise<Result<Cost | null, CostError>>;
  /** Turns whose prompt-cache reads dropped sharply, each with its likely cause. */
  readonly cacheBreaks: () => Promise<Result<readonly CacheBreak[], ReadError>>;
};

export function usageMethods(
  log: LogStore,
  threadId: ThreadId,
  branchId: BranchId,
): ThreadUsage {
  return {
    usage: async () => {
      const read = await readLog(log, branchId);
      return read.ok ? ok(reduce(read.value, log.now()).usage) : read;
    },
    cost: async (options = {}) => {
      const read = await readLog(log, branchId);
      if (!read.ok) return read;
      const total = await (options.tree === true
        ? treeCost(log, threadId, read.value)
        : ownCost(read.value));
      return total.ok ? ok(total.value ?? null) : total;
    },
    cacheBreaks: async () => {
      const read = await readLog(log, branchId);
      if (!read.ok) return read;
      // The effective ttl, so an agent that pinned no context still gets a list.
      const { cache_ttl_ms } = contextPolicy(read.value.fold.policy);
      return ok(cacheBreaks(knownEvents(read.value), cache_ttl_ms));
    },
  };
}
