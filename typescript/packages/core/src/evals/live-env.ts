import { z } from "zod";
import { ConfigError } from "../agent/errors";
import type { RunResult, ThreadRef } from "../agent/result";
import { openStore, type Store } from "../agent/sqlite";
import type { Budget, Cost, KnownEvent, Principal } from "../log";
import type { Model } from "../model";
import { ModelBlockedError } from "../model/guard";
import { knownEvents } from "../reduce";
import type { Thread } from "../thread";
import { addCost } from "./report";
import type { JudgeCheck, Simulation } from "./schema";

// What the live checks share (spec lane 22, C and lane 32, B): the options a live run is given,
// the eval principal, the per-case outcome as a value, and reading a live thread's log back.

/** What `live` / `--live` gives: the judge model, the budget of every run, extra criteria. */
export type Live = {
  readonly judge: Model;
  readonly budget: z.infer<typeof Budget>;
  readonly rubric?: readonly string[];
  /** The model that plays the user of a `kind: "model"` simulated case (spec lane 32). */
  readonly user?: Model;
};

export const EVAL_PRINCIPAL: Principal = {
  issuer: "threads",
  tenant: "evals",
  subject: "eval-runner",
};

export type Calls = {
  readonly agent: number;
  readonly user: number;
  readonly judge: number;
};

export const NO_CALLS: Calls = { agent: 0, user: 0, judge: 0 };

export type LiveOutcome =
  | {
      readonly kind: "graded";
      readonly check: JudgeCheck;
      readonly calls: Calls;
      readonly cost: Cost | null;
      readonly simulation?: Simulation;
    }
  | {
      readonly kind: "error" | "skipped";
      readonly reason: string;
      readonly calls: Calls;
      readonly cost: Cost | null;
      readonly simulation?: Simulation;
    }
  | { readonly kind: "blocked"; readonly model: string };

export type Env = {
  readonly live: Live;
  readonly store: Store;
  /** Thread ids are reported only when the threads are kept (a store the caller passed). */
  readonly kept: boolean;
  /**
   * The agent runs on a fresh sandbox: a live eval never restores the case's snapshot, which
   * lives in the provider the case was recorded on.
   */
  readonly fresh: boolean;
};

/** A thread's events, read from the store: the log is the truth the report is derived from. */
export async function eventsOf(
  store: Store,
  thread: ThreadRef,
): Promise<readonly KnownEvent[]> {
  const { log } = await openStore(store);
  const read = await log.read(thread.branch);
  return read.ok ? knownEvents(read.value) : [];
}

/** Thread.cost({tree: true}) summed; null once any part ran unpriced. */
export async function costOf(threads: readonly Thread[]): Promise<Cost | null> {
  let total: Cost | null | undefined;
  for (const t of threads) {
    const got = await t.cost({ tree: true });
    total = addCost(total, got.ok ? got.value : null);
  }
  return total ?? null;
}

/** RunResult.output as the judge sees it: JSON when the agent has an output schema, else text. */
export function answerOf(output: unknown): z.core.util.JSONType {
  return z.json().parse(output ?? null);
}

/** A run that didn't complete, as the case's error reason. */
export function unfinished(
  r: Exclude<RunResult<unknown>, { status: "completed" }>,
): string {
  return r.status === "failed" ? `failed: ${r.error.code}` : r.status;
}

/** A guard block and a setup mistake are values; any other throw is a bug. */
export async function guarded<T>(
  run: () => Promise<T>,
): Promise<T | { readonly blocked: string } | { readonly config: string }> {
  try {
    return await run();
  } catch (error) {
    if (error instanceof ModelBlockedError) return { blocked: error.model };
    if (error instanceof ConfigError)
      return { config: `${error.code}: ${error.message}` };
    throw error;
  }
}
