import type { EventOf } from "../fold/state";
import type { Cost, KnownEvent, ThreadId } from "../log";
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
type Finished = EventOf<"agent_finished">;

/**
 * spec/schema/README.md, Subagent cancellation: a child with no thread is recorded cancelled,
 * with unknown usage, and never created. A started child cancelled with unknown usage writes the
 * same record, so it can't prove the child spent nothing.
 */
const neverCreated = ({ data }: Finished): boolean =>
  data.status === "cancelled" &&
  data.usage.input_tokens === null &&
  data.usage.output_tokens === null;

/** A spawned child still to read and walk, with the path of child ids that leads to it. */
type Pending = {
  readonly spawn: Spawned;
  readonly finish: Finished | undefined;
  readonly path: string;
};

/**
 * Every thread of the tree rooted at `root`, depth first in spawn order, with an explicit stack
 * so a tree of any depth is walked, and each child read only on its turn, so the first broken
 * path depth first is the one reported. Each child must name, as its parent, the agent_spawned
 * that started it; a child that doesn't, or a thread named twice (a cycle, or two spawns of one
 * id), makes the tree log_corrupt. A child with no log counts as an unpriced thread that ran:
 * nothing proves it spent nothing (its log may have been deleted), so the total is incomplete
 * and unbounded, never falsely complete.
 */
function treeParts(
  log: LogStore,
  root: ThreadId,
  chain: VerifiedLog,
): Result<readonly TreePart[], CostError> {
  const parts: TreePart[] = [];
  const seen = new Set<ThreadId>([root]);
  const stack: Pending[] = [];
  let next: { readonly at: VerifiedLog; readonly path: string } | undefined = {
    at: chain,
    path: "",
  };
  while (next !== undefined) {
    const own = ownCost(next.at);
    if (!own.ok) return err(within(next.path, own.error));
    const events = knownEvents(next.at);
    parts.push({
      cost: own.value,
      ran: events.some((e) => e.type === "model_request"),
    });
    stack.push(...pending(events, next.path).toReversed());
    const read = nextChild(log, stack, seen, parts);
    if (!read.ok) return read;
    next = read.value;
  }
  return ok(parts);
}

/** The spawns of `events`, in order, each with the parent's agent_finished for its child. */
function pending(events: readonly KnownEvent[], path: string): Pending[] {
  const finishes = new Map<ThreadId, Finished>();
  for (const e of events)
    if (e.type === "agent_finished") finishes.set(e.data.child_thread_id, e);
  return events.flatMap((e) =>
    e.type === "agent_spawned"
      ? [
          {
            spawn: e,
            finish: finishes.get(e.data.child_thread_id),
            path: `${path}child ${e.data.child_thread_id}: `,
          },
        ]
      : [],
  );
}

/**
 * Pops pending children until one has a log to walk; each without a log adds its unpriced part
 * instead. Undefined when the stack is empty.
 */
function nextChild(
  log: LogStore,
  stack: Pending[],
  seen: Set<ThreadId>,
  parts: TreePart[],
): Result<{ at: VerifiedLog; path: string } | undefined, CostError> {
  for (let top = stack.pop(); top !== undefined; top = stack.pop()) {
    const child = top.spawn.data.child_thread_id;
    if (seen.has(child))
      return err(
        readError(
          "log_corrupt",
          `${top.path}thread ${child} appears twice in the tree`,
        ),
      );
    seen.add(child);
    const read = childLog(log, top.spawn, top.finish);
    if (!read.ok) return err(within(top.path, read.error));
    if (read.value !== undefined) return ok({ at: read.value, path: top.path });
    parts.push({ cost: undefined, ran: true });
  }
  return ok(undefined);
}

/**
 * The spawned child's log; undefined when it has no thread and its parent's record allows that:
 * no agent_finished, or the one a cancelled parent writes for a child it never created. Any
 * other finished child's missing log is log_corrupt.
 */
function childLog(
  log: LogStore,
  spawn: Spawned,
  finish: Finished | undefined,
): Result<VerifiedLog | undefined, ReadError> {
  const branch = log.mainBranch(spawn.data.child_thread_id);
  const unstarted = finish === undefined || neverCreated(finish);
  if (!branch.ok && branch.error.code === "branch_not_found" && unstarted)
    return ok(undefined);
  if (!branch.ok)
    return err(
      readError(
        "log_corrupt",
        finish === undefined
          ? branch.error.message
          : `its log is missing, though its parent recorded it ${finish.data.status}`,
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
function within<E extends { readonly message: string }>(
  path: string,
  error: E,
): E {
  return path === "" ? error : { ...error, message: `${path}${error.message}` };
}
