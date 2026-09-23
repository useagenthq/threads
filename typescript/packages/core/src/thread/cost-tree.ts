import type { EventOf } from "../fold/state";
import type { Cost, ThreadId } from "../log";
import {
  type CostOverflow,
  cost,
  knownEvents,
  mergeTree,
  type TreePart,
} from "../reduce";
import { err, ok, type Result } from "../result";
import type { LogStore } from "../store";
import type { VerifiedLog } from "../verify";
import { type ReadError, readError, readLog } from "./read";

// Thread.cost({ tree: true }) (spec/api.json): the tree merge over this thread and every
// descendant, each read from its own main branch, so the logs stay the only truth (the budget
// ledger is a cache, never reused here).

/** Thread.cost's failures: a read error anywhere in the tree, or cost_overflow. */
export type CostError =
  | ReadError
  | { readonly code: CostOverflow["error"]; readonly message: string };

/** The thread's own cost projection, or cost_overflow past the wire's integers. */
export function ownCost(
  chain: VerifiedLog,
): Result<Cost | undefined, CostError> {
  return costResult(cost(knownEvents(chain), chain.fold.policy));
}

function costResult(
  total: Cost | CostOverflow | undefined,
): Result<Cost | undefined, CostError> {
  return total !== undefined && "error" in total
    ? err({
        code: total.error,
        message: "the cost exceeds 2^53 - 1 nanos",
      })
    : ok(total);
}

export function treeCost(
  log: LogStore,
  root: ThreadId,
  chain: VerifiedLog,
): Result<Cost | undefined, CostError> {
  const parts = treeParts(log, root, chain);
  return parts.ok ? costResult(mergeTree(parts.value)) : parts;
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
): Result<readonly TreePart[], CostError> {
  const parts: TreePart[] = [];
  const seen = new Set<ThreadId>();
  const visit = (id: ThreadId, at: VerifiedLog): Result<void, CostError> => {
    if (seen.has(id))
      return err(
        readError("log_corrupt", `thread ${id} appears twice in the tree`),
      );
    seen.add(id);
    const own = ownCost(at);
    if (!own.ok) return own;
    const events = knownEvents(at);
    parts.push({
      cost: own.value,
      ran: events.some((e) => e.type === "model_request"),
    });
    for (const spawn of events.filter((e) => e.type === "agent_spawned")) {
      const child = spawn.data.child_thread_id;
      const finished = events.some(
        (e) => e.type === "agent_finished" && e.data.child_thread_id === child,
      );
      const read = childLog(log, spawn, finished);
      const below =
        read.ok && read.value !== undefined ? visit(child, read.value) : read;
      if (!below.ok) return err(inChild(child, below.error));
    }
    return ok(undefined);
  };
  const walked = visit(root, chain);
  return walked.ok ? ok(parts) : walked;
}

/**
 * The spawned child's log; undefined when it has no thread and its parent never recorded it
 * finishing, so it never started. A finished child's missing log is log_corrupt: counting
 * nothing for it would be a partial sum.
 */
function childLog(
  log: LogStore,
  spawn: Spawned,
  finished: boolean,
): Result<VerifiedLog | undefined, ReadError> {
  const branch = log.mainBranch(spawn.data.child_thread_id);
  if (!branch.ok && branch.error.code === "branch_not_found" && !finished)
    return ok(undefined);
  if (!branch.ok)
    return err(
      readError(
        "log_corrupt",
        finished
          ? "its log is missing, though its parent recorded agent_finished for it"
          : branch.error.message,
      ),
    );
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
        readError(
          "log_corrupt",
          "its thread_started doesn't name the agent_spawned that started it",
        ),
      );
}

/** The error, keeping its code, with the path to the descendant that failed. */
function inChild<E extends { readonly message: string }>(
  child: string,
  error: E,
): E {
  return { ...error, message: `child ${child}: ${error.message}` };
}
