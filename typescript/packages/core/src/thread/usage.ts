import type { EventOf } from "../fold/state";
import type { BranchId, CacheBreak, Cost, ThreadId } from "../log";
import { contextPolicy } from "../loop/policy";
import {
  cacheBreaks,
  cost,
  knownEvents,
  mergeTree,
  type ReducedState,
  reduce,
  type TreePart,
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
   * conservative upper bound; null when the thread pins no prices. `tree: true` adds every
   * descendant subagent, in the root's currency (else the first priced descendant's; null when
   * none is priced). A thread that spent money it can't add (unpriced, or another currency)
   * makes the total incomplete and unbounded; one that made no model request changes nothing.
   */
  readonly cost: (options?: {
    readonly tree?: boolean;
  }) => Promise<Result<Cost | null, LogError>>;
  /** Turns whose prompt-cache reads dropped sharply, each with its likely cause. */
  readonly cacheBreaks: () => Promise<Result<readonly CacheBreak[], LogError>>;
};

export function usageMethods(
  log: LogStore,
  threadId: ThreadId,
  branchId: BranchId,
): ThreadUsage {
  return {
    usage: async () => {
      const read = readLog(log, branchId);
      return read.ok ? ok(reduce(read.value, log.now()).usage) : read;
    },
    cost: async (options = {}) => {
      const read = readLog(log, branchId);
      if (!read.ok) return read;
      if (options.tree !== true) return ok(ownCost(read.value) ?? null);
      const parts = treeParts(log, threadId, read.value);
      return parts.ok ? ok(mergeTree(parts.value) ?? null) : parts;
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

type Spawned = EventOf<"agent_spawned">;

/**
 * Every thread of the tree rooted at `root`, depth first in spawn order. Each child is read from
 * its own main branch and must name, as its parent, the agent_spawned that started it; a child
 * that doesn't, or a thread met twice (a cycle), makes the tree log_corrupt.
 */
function treeParts(
  log: LogStore,
  root: ThreadId,
  chain: VerifiedLog,
): Result<readonly TreePart[], LogError> {
  const parts: TreePart[] = [];
  const seen = new Set<ThreadId>();
  const visit = (id: ThreadId, at: VerifiedLog): Result<void, LogError> => {
    if (seen.has(id))
      return err(
        logError("log_corrupt", `thread ${id} appears twice in the tree`),
      );
    seen.add(id);
    const events = knownEvents(at);
    parts.push({
      cost: ownCost(at),
      ran: events.some((e) => e.type === "model_request"),
    });
    for (const spawn of events.filter((e) => e.type === "agent_spawned")) {
      const child = spawn.data.child_thread_id;
      const read = childLog(log, spawn);
      const below =
        read.ok && read.value !== undefined ? visit(child, read.value) : read;
      if (!below.ok) return err(inChild(child, below.error));
    }
    return ok(undefined);
  };
  const walked = visit(root, chain);
  return walked.ok ? ok(parts) : walked;
}

/** The spawned child's log; undefined when it has no thread yet, so it never started. */
function childLog(
  log: LogStore,
  spawn: Spawned,
): Result<VerifiedLog | undefined, LogError> {
  const branch = log.mainBranch(spawn.data.child_thread_id);
  if (!branch.ok)
    return branch.error.code === "branch_not_found" ? ok(undefined) : branch;
  const read = readLog(log, branch.value);
  if (!read.ok) return read;
  const started = knownEvents(read.value).find(
    (e) => e.type === "thread_started",
  );
  const link =
    started?.type === "thread_started" ? started.data.parent : undefined;
  const backlinked =
    link?.relation === "subagent" &&
    link.thread_id === spawn.thread_id &&
    link.branch_id === spawn.branch_id &&
    link.event_id === spawn.event_id;
  return backlinked
    ? read
    : err(
        logError(
          "log_corrupt",
          "its thread_started doesn't name the agent_spawned that started it",
        ),
      );
}

/** The error, keeping its code, with the path to the descendant that failed. */
function inChild(child: string, error: LogError): LogError {
  return logError(error.code, `child ${child}: ${error.message}`, error.seq);
}
