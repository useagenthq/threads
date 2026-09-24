import { z } from "zod";
import type { Agent } from "../agent/agent";
import { agent } from "../agent/agent";
import { ConfigError } from "../agent/errors";
import { liveRunOf } from "../agent/registry";
import type { RunResult, ThreadRef } from "../agent/result";
import { openStore, type Store } from "../agent/sqlite";
import type { Budget, Cost, KnownEvent, Principal } from "../log";
import { recordedStubs } from "../loop";
import type { Model } from "../model";
import { ModelBlockedError } from "../model/guard";
import { knownEvents } from "../reduce";
import type { Thread } from "../thread";
import type { CaseDir } from "./case-dir";
import {
  type Graded,
  JUDGE_V1,
  judgeInput,
  transcript,
  verdicts,
} from "./judge";
import { addCost } from "./report";
import { type JudgeCheck, Verdicts } from "./schema";

// The live check (spec lane 22, C): the current agent answers the case input on a new thread,
// with every mediated call answered from the recorded stubs (an eval never performs a real side
// effect), and a judge model grades the whole turn against the rubric. Real model calls, so it
// runs only when asked, under a budget.

/** What `live` / `--live` gives: the judge model, the budget of every run, extra criteria. */
export type Live = {
  readonly judge: Model;
  readonly budget: z.infer<typeof Budget>;
  readonly rubric?: readonly string[];
};

export const EVAL_PRINCIPAL: Principal = {
  issuer: "threads",
  tenant: "evals",
  subject: "eval-runner",
};

export type LiveOutcome =
  | {
      readonly kind: "graded";
      readonly check: JudgeCheck;
      readonly calls: { readonly agent: number; readonly judge: number };
      readonly cost: Cost | null;
    }
  | {
      readonly kind: "error" | "skipped";
      readonly reason: string;
      readonly calls: { readonly agent: number; readonly judge: number };
      readonly cost: Cost | null;
    }
  | { readonly kind: "blocked"; readonly model: string };

type Env = {
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

const NONE = { agent: 0, judge: 0 };

/** A thread's events, read from the store: the log is the truth the report is derived from. */
async function eventsOf(
  store: Store,
  thread: ThreadRef,
): Promise<readonly KnownEvent[]> {
  const { log } = await openStore(store);
  const read = log.read(thread.branch);
  return read.ok ? knownEvents(read.value) : [];
}

/** model_request events of a thread and its subagents, depth first. */
async function requests(store: Store, thread: ThreadRef): Promise<number> {
  const { log } = await openStore(store);
  const events = await eventsOf(store, thread);
  let count = events.filter((e) => e.type === "model_request").length;
  for (const e of events) {
    if (e.type !== "agent_spawned") continue;
    const branch = log.mainBranch(e.data.child_thread_id);
    if (branch.ok)
      count += await requests(store, {
        ...thread,
        id: e.data.child_thread_id,
        branch: branch.value,
      });
  }
  return count;
}

/** Thread.cost({tree: true}) summed; null once any part ran unpriced. */
async function costOf(threads: readonly Thread[]): Promise<Cost | null> {
  let total: Cost | null | undefined;
  for (const t of threads) {
    const got = await t.cost({ tree: true });
    total = addCost(total, got.ok ? got.value : null);
  }
  return total ?? null;
}

/** A run that didn't complete, as the case's error reason. */
function unfinished(
  r: Exclude<RunResult<unknown>, { status: "completed" }>,
): string {
  return r.status === "failed" ? `failed: ${r.error.code}` : r.status;
}

async function guarded<T>(
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

async function judged(
  c: CaseDir,
  criteria: readonly string[],
  lead: Extract<RunResult<unknown>, { status: "completed" }>,
  env: Env,
): Promise<LiveOutcome> {
  const text = c.meta.input?.text ?? "";
  const events = await eventsOf(env.store, lead.thread);
  const answer = z.json().parse(lead.output ?? null);
  const judge = agent({
    name: "judge",
    model: env.live.judge,
    instructions: JUDGE_V1,
    output: Verdicts,
    outputRetries: 1,
  });
  const input = judgeInput({ task: text, events, answer, rubric: criteria });
  const ran = await guarded(() =>
    judge.run(input, {
      store: env.store,
      principal: EVAL_PRINCIPAL,
      budget: env.live.budget,
    }),
  );
  if ("blocked" in ran) return { kind: "blocked", model: ran.blocked };
  const calls = {
    agent: await requests(env.store, lead.thread),
    judge: "config" in ran ? 0 : await requests(env.store, ran.thread),
  };
  const cost = await costOf(
    "config" in ran ? [lead.thread] : [lead.thread, ran.thread],
  );
  if ("config" in ran)
    return { kind: "error", reason: ran.config, calls, cost };
  if (ran.status === "budget_exhausted")
    return { kind: "error", reason: "budget_exhausted", calls, cost };
  const graded =
    ran.status === "completed" ? verdicts(ran.output, criteria) : undefined;
  if (graded === undefined || !graded.ok)
    return { kind: "error", reason: "judge_invalid", calls, cost };
  return {
    kind: "graded",
    check: checkOf(graded.value, answer, events, lead, ran, env),
    calls,
    cost,
  };
}

function checkOf(
  graded: readonly Graded[],
  answer: z.core.util.JSONType,
  events: readonly KnownEvent[],
  lead: RunResult<unknown>,
  judge: RunResult<unknown>,
  env: Env,
): JudgeCheck {
  const passed = graded.filter((v) => v.pass).length;
  return {
    answer,
    transcript_items: transcript(events).length,
    score: passed / graded.length,
    fresh_sandbox: env.fresh,
    verdicts: [...graded],
    ...(env.kept
      ? { thread_id: lead.thread.id, judge_thread_id: judge.thread.id }
      : {}),
  };
}

/** Runs the case input on the current agent and grades it. */
export async function liveCheck(
  c: CaseDir,
  target: Agent<never, unknown>,
  env: Env,
): Promise<LiveOutcome> {
  const criteria = [...(c.meta.rubric ?? []), ...(env.live.rubric ?? [])];
  if (criteria.length === 0)
    return { kind: "skipped", reason: "no_rubric", calls: NONE, cost: null };
  const text = c.meta.input?.text;
  const run = liveRunOf(target);
  if (text === undefined || run === undefined)
    return {
      kind: "skipped",
      reason: "offline_not_runnable:content_input",
      calls: NONE,
      cost: null,
    };
  const stub = recordedStubs(c.stubs ?? { stubs: [] });
  const lead = await guarded(() =>
    run(text, {
      store: env.store,
      principal: EVAL_PRINCIPAL,
      budget: env.live.budget,
      stub,
    }),
  );
  if ("blocked" in lead) return { kind: "blocked", model: lead.blocked };
  if ("config" in lead)
    return { kind: "error", reason: lead.config, calls: NONE, cost: null };
  if (lead.status !== "completed") {
    const calls = { agent: await requests(env.store, lead.thread), judge: 0 };
    return {
      kind: "error",
      reason: unfinished(lead),
      calls,
      cost: await costOf([lead.thread]),
    };
  }
  return judged(c, criteria, lead, env);
}
