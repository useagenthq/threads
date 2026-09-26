import type { Agent } from "../agent/agent";
import { liveRunOf } from "../agent/registry";
import type { RunResult } from "../agent/result";
import type { CaseDir } from "./case-dir";
import type { CaseLog } from "./checks";
import { JUDGE_V1, transcript } from "./judge";
import { runJudge } from "./judge-run";
import {
  answerOf,
  costOf,
  type Env,
  EVAL_PRINCIPAL,
  eventsOf,
  guarded,
  type LiveOutcome,
  NO_CALLS,
  unfinished,
} from "./live-env";
import { simulateCase } from "./simulate";
import { stubQueue } from "./stub-queue";
import { treeTotals } from "./tree-totals";

// The live check (spec lane 22, C): the current agent answers the case input on a new thread,
// with every mediated call answered from the recorded stubs (an eval never performs a real side
// effect), and a judge model grades the whole turn against the rubric. A case with `simulate`
// goes to the multi-turn conversation instead (spec lane 32, B).

export type { Env, Live, LiveOutcome } from "./live-env";
export { EVAL_PRINCIPAL } from "./live-env";

async function judged(
  c: CaseDir,
  criteria: readonly string[],
  lead: Extract<RunResult<unknown>, { status: "completed" }>,
  env: Env,
): Promise<LiveOutcome> {
  const events = await eventsOf(env.store, lead.thread);
  const answer = answerOf(lead.output);
  const items = transcript(events);
  const ran = await runJudge(
    {
      instructions: JUDGE_V1,
      task: c.meta.input?.text ?? "",
      events,
      answer,
      rubric: criteria,
    },
    env,
  );
  if (ran.kind === "blocked") return { kind: "blocked", model: ran.model };
  const calls = {
    agent: (await treeTotals(env.store, lead.thread)).requests,
    user: 0,
    judge: ran.calls,
  };
  const cost = await costOf(
    ran.thread === undefined ? [lead.thread] : [lead.thread, ran.thread],
  );
  if (ran.kind === "error")
    return { kind: "error", reason: ran.reason, calls, cost };
  const passed = ran.graded.filter((v) => v.pass).length;
  return {
    kind: "graded",
    check: {
      answer,
      transcript_items: items.length,
      score: passed / ran.graded.length,
      fresh_sandbox: env.fresh,
      verdicts: [...ran.graded],
      ...(env.kept
        ? { thread_id: lead.thread.id, judge_thread_id: ran.thread.id }
        : {}),
    },
    calls,
    cost,
  };
}

/** Runs the case input on the current agent and grades it. */
export async function liveCheck(
  c: CaseDir,
  log: CaseLog,
  target: Agent<never, unknown>,
  env: Env,
): Promise<LiveOutcome> {
  const criteria = [...(c.meta.rubric ?? []), ...(env.live.rubric ?? [])];
  if (criteria.length === 0)
    return {
      kind: "skipped",
      reason: "no_rubric",
      calls: NO_CALLS,
      cost: null,
    };
  if (c.meta.simulate !== undefined)
    return simulateCase(c, log, target, criteria, c.meta.simulate, env);
  const text = c.meta.input?.text;
  const run = liveRunOf(target);
  if (text === undefined || run === undefined)
    return {
      kind: "skipped",
      reason: "offline_not_runnable:content_input",
      calls: NO_CALLS,
      cost: null,
    };
  const lead = await guarded(() =>
    run(text, {
      store: env.store,
      principal: EVAL_PRINCIPAL,
      budget: env.live.budget,
      stub: stubQueue(c.stubs ?? { stubs: [] }, "turn"),
    }),
  );
  if ("blocked" in lead) return { kind: "blocked", model: lead.blocked };
  if ("config" in lead)
    return { kind: "error", reason: lead.config, calls: NO_CALLS, cost: null };
  if (lead.status !== "completed") {
    const calls = {
      agent: (await treeTotals(env.store, lead.thread)).requests,
      user: 0,
      judge: 0,
    };
    return {
      kind: "error",
      reason: unfinished(lead),
      calls,
      cost: await costOf([lead.thread]),
    };
  }
  return judged(c, criteria, lead, env);
}
