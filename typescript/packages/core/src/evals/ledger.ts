import type { ThreadRef } from "../agent/result";
import { openStore } from "../agent/sqlite";
import type { Cost } from "../log";
import type { Thread } from "../thread";
import { threadHandle } from "../thread/handle";
import { costOf, type Env } from "./live-env";
import type { Spent } from "./remaining-budget";
import { NO_TOTALS, treeTotals } from "./tree-totals";

// What a simulated conversation has spent so far (spec lane 32, B.5). A continued prefix is
// already on the thread, so the ledger starts at the imported log's seq and cost: only what
// this conversation appends is charged to its budget. One monotonic clock covers both threads.

export type Ledger = {
  /** Events at or below this seq on the agent thread are the imported prefix. */
  readonly since: number;
  readonly baseCostNanos: number;
  readonly startedMs: number;
  readonly clock: () => number;
};

export function startLedger(
  since: number,
  baseCostNanos: number,
  clock: () => number = () => performance.now(),
): Ledger {
  return { since, baseCostNanos, startedMs: clock(), clock };
}

/** The upper bound of a thread's cost, the bound unknown usage is charged at. */
export async function costBound(thread: Thread): Promise<number> {
  const got = await thread.cost({ tree: true });
  return got.ok ? (got.value?.upper_bound_nanos ?? 0) : 0;
}

export async function handleFor(env: Env, ref: ThreadRef): Promise<Thread> {
  return threadHandle(await openStore(env.store), ref);
}

/** Both threads' totals since the ledger started, as the budget arithmetic reads them. */
export async function spentSoFar(
  env: Env,
  led: Ledger,
  agent: ThreadRef | undefined,
  user: Thread | undefined,
): Promise<Spent> {
  const a =
    agent === undefined
      ? NO_TOTALS
      : await treeTotals(env.store, agent, led.since);
  const u = user === undefined ? NO_TOTALS : await treeTotals(env.store, user);
  const threads = [
    ...(agent === undefined ? [] : [await handleFor(env, agent)]),
    ...(user === undefined ? [] : [user]),
  ];
  const cost: Cost | null = await costOf(threads);
  return {
    requests: a.requests + u.requests,
    inputTokens: a.inputTokens + u.inputTokens,
    outputTokens: a.outputTokens + u.outputTokens,
    turns: a.turns + u.turns,
    costNanos: Math.max(0, (cost?.upper_bound_nanos ?? 0) - led.baseCostNanos),
    wallMs: led.clock() - led.startedMs,
  };
}
