import { agent } from "../agent/agent";
import type { Thread } from "../thread";
import { type Graded, type JudgeTask, judgeInput, verdicts } from "./judge";
import { type Env, EVAL_PRINCIPAL, guarded } from "./live-env";
import { Verdicts } from "./schema";
import { treeTotals } from "./tree-totals";

// One judge run (spec lane 22, C.2-C.4 and lane 32, D). The instructions and the transcript
// differ between a single graded turn and a simulated conversation; everything else -- the
// budget, the strict verdict rule and the failure-as-a-value shape -- is the same.

export type JudgeAsk = JudgeTask & { readonly instructions: string };

export type Judged =
  | {
      readonly kind: "graded";
      readonly graded: readonly Graded[];
      readonly thread: Thread;
      readonly calls: number;
    }
  | {
      readonly kind: "error";
      readonly reason: string;
      readonly thread: Thread | undefined;
      readonly calls: number;
    }
  | { readonly kind: "blocked"; readonly model: string };

export async function runJudge(ask: JudgeAsk, env: Env): Promise<Judged> {
  const judge = agent({
    name: "judge",
    model: env.live.judge,
    instructions: ask.instructions,
    output: Verdicts,
    outputRetries: 1,
  });
  const ran = await guarded(() =>
    judge.run(judgeInput(ask), {
      store: env.store,
      principal: EVAL_PRINCIPAL,
      budget: env.live.budget,
    }),
  );
  if ("blocked" in ran) return { kind: "blocked", model: ran.blocked };
  if ("config" in ran)
    return { kind: "error", reason: ran.config, thread: undefined, calls: 0 };
  const calls = (await treeTotals(env.store, ran.thread)).requests;
  if (ran.status === "budget_exhausted")
    return {
      kind: "error",
      reason: "budget_exhausted",
      thread: ran.thread,
      calls,
    };
  const graded =
    ran.status === "completed" ? verdicts(ran.output, ask.rubric) : undefined;
  if (graded === undefined || !graded.ok)
    return {
      kind: "error",
      reason: "judge_invalid",
      thread: ran.thread,
      calls,
    };
  return { kind: "graded", graded: graded.value, thread: ran.thread, calls };
}
