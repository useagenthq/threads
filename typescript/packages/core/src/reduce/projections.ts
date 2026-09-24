import type { Fold, Todo } from "../fold/state";
import type { CacheBreak, Cost, KnownEvent } from "../log";
import { pendingWakes } from "../store/wakes";
import type { Chain } from "../verify/chain";
import { type CostOverflow, cost } from "./cost";
import { knownEvents } from "./reduce";
import { runProjection } from "./run-end";

/** The events a cache break is attributed to, checked against the schema's causes. */
const CAUSES = [
  "settings_changed",
  "compacted",
  "context_edited",
  "tools_changed",
] as const satisfies readonly CacheBreak["likely_cause"][];
type Cause = (typeof CAUSES)[number];

/** Named projections beyond ReducedState (spec/conformance/README.md, "Projections"). */
export type Projections = {
  readonly cost: Cost | CostOverflow | undefined;
  readonly cache_breaks: readonly CacheBreak[] | undefined;
  readonly compaction:
    | { readonly consecutive_failures: number; readonly breaker_open: boolean }
    | undefined;
  readonly todos: readonly Todo[];
  readonly children: readonly {
    readonly child_thread_id: string;
    readonly status: string;
  }[];
  readonly team_tasks: readonly {
    readonly task_id: string;
    readonly status: string;
    readonly owner?: string;
  }[];
  readonly mode: Fold["mode"];
  readonly model: Fold["model"];
  readonly output: Fold["output"];
  readonly run: ReturnType<typeof runProjection>;
  readonly pending_wakes: readonly {
    readonly branch_id: string;
    readonly child_thread_id: string;
  }[];
};

export function projections(chain: Chain): Projections {
  const { fold } = chain;
  const events = knownEvents(chain);
  const maxFailures = fold.policy?.context?.compact.max_failures;
  const ttl = fold.policy?.context?.cache_ttl_ms;
  return {
    cost: cost(events, fold.policy),
    cache_breaks: ttl === undefined ? undefined : cacheBreaks(events, ttl),
    compaction:
      maxFailures === undefined
        ? undefined
        : {
            consecutive_failures: fold.compactionFailures,
            breaker_open: fold.compactionFailures >= maxFailures,
          },
    todos: fold.todos,
    children: [...fold.children].map(([id, status]) => ({
      child_thread_id: id,
      status,
    })),
    team_tasks: [...fold.tasks].map(([id, task]) =>
      task.owner === undefined
        ? { task_id: id, status: task.status }
        : { task_id: id, status: task.status, owner: task.owner },
    ),
    mode: fold.mode,
    model: fold.model,
    output: fold.output,
    run: runProjection(events),
    pending_wakes: wakeRows(chain, events),
  };
}

/** The pending_wakes rows the branch's own events rebuild, sorted. */
function wakeRows(
  chain: Chain,
  events: readonly KnownEvent[],
): Projections["pending_wakes"] {
  const branch = chain.segments.at(-1)?.header.branch_id ?? "";
  return pendingWakes(events, branch)
    .toSorted()
    .map((child) => ({ branch_id: branch, child_thread_id: child }));
}

const DROP_MIN = 2000;

/**
 * A turn response whose cache reads fell below 95% of the previous turn response's, by at
 * least 2000 tokens, attributed to the first cause between them.
 */
export function cacheBreaks(
  events: readonly KnownEvent[],
  ttl: number,
): readonly CacheBreak[] {
  const turnRequests = new Map(
    events.flatMap((e) =>
      e.type === "model_request" && e.data.purpose !== "compaction"
        ? [[e.event_id, e.time] as const]
        : [],
    ),
  );
  const out: CacheBreak[] = [];
  let previous: { reads: number; time: number } | undefined;
  let seen: Cause[] = [];
  for (const e of events) {
    if (isCause(e.type)) seen.push(e.type);
    if (e.type !== "model_response" && e.type !== "model_response_recovered")
      continue;
    const requestId = e.data.request_event_id;
    const requestTime = turnRequests.get(requestId);
    const reads = e.data.usage.cache_read_tokens;
    if (requestTime === undefined || typeof reads !== "number") continue;
    const current = { reads, time: e.time };
    const cause = breakCause(previous, current, requestTime, seen, ttl);
    if (cause !== undefined)
      out.push({ request_event_id: requestId, likely_cause: cause });
    previous = current;
    seen = [];
  }
  return out;
}

type Reads = { readonly reads: number; readonly time: number };

/** Why cache reads dropped since the previous turn response, or undefined if they didn't. */
function breakCause(
  previous: Reads | undefined,
  current: Reads,
  requestTime: number,
  seen: readonly Cause[],
  ttl: number,
): CacheBreak["likely_cause"] | undefined {
  if (previous === undefined) return undefined;
  // Exact: products of counts near 2^53 round as doubles, which could hide a drop.
  const dropped =
    20n * BigInt(current.reads) < 19n * BigInt(previous.reads) &&
    previous.reads - current.reads >= DROP_MIN;
  if (!dropped) return undefined;
  return (
    seen[0] ?? (requestTime - previous.time > ttl ? "ttl_expired" : "unknown")
  );
}

function isCause(type: string): type is Cause {
  return CAUSES.some((c) => c === type);
}
