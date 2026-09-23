import type { z } from "zod";
import type { Budget, Policy } from "../log";
import type { Model } from "../model";
import { tokenBounds } from "../reduce/cost";
import { ConfigError } from "./errors";
import { enforcement } from "./registry";

// spec/schema/README.md, Budget enforcement: a limit is reserved per attempt at the attempt's
// bound, so setup refuses a limit that one of the agent's models can't bound, unless
// on_unknown_usage is stop: the run-time check then refuses the unbounded attempt instead.

/** Throws budget_unenforceable when some model has no per-attempt bound for a set limit. */
export function checkEnforceable(
  budget: z.infer<typeof Budget> | undefined,
  models: readonly Model[],
  onUnknownUsage: Policy["on_unknown_usage"],
): void {
  if (budget === undefined || onUnknownUsage === "stop") return;
  for (const { info } of models) {
    const bounds = tokenBounds(info.limits, info.params, undefined);
    const missing = [
      budget.max_input_tokens !== undefined &&
        bounds.input === undefined &&
        "max_input_tokens",
      budget.max_output_tokens !== undefined &&
        bounds.output === undefined &&
        "max_output_tokens",
      budget.max_cost_nanos !== undefined &&
        (bounds.input === undefined ||
          bounds.output === undefined ||
          info.limits.price === undefined) &&
        "max_cost_nanos",
    ].find((limit) => limit !== false);
    if (missing !== undefined)
      throw new ConfigError(
        "budget_unenforceable",
        `${missing} can't be enforced: model ${info.model.provider}/${info.model.name} has no per-attempt bound for it`,
      );
  }
}

/** What setup reads of an agent to check the limits over its whole tree. */
export type Enforceable = {
  readonly budget: z.infer<typeof Budget> | undefined;
  readonly model: Model;
  readonly fallback: readonly Model[];
  readonly onUnknownUsage: Policy["on_unknown_usage"];
  /** Its subagents and handoff targets, whose threads the same limits cover. */
  readonly agents: readonly object[];
  readonly targets: readonly object[];
};

/**
 * checkEnforceable over every thread the agent may start: each limit covering it (`covering`
 * from a run or an ancestor, and its own budget) is checked against its models, then passed
 * down to its subagents and handoff targets (spec/schema/README.md, Budget enforcement).
 */
export function checkTree(
  def: Enforceable,
  covering: readonly z.infer<typeof Budget>[] = [],
): void {
  const over = def.budget === undefined ? covering : [...covering, def.budget];
  for (const budget of over)
    checkEnforceable(budget, [def.model, ...def.fallback], def.onUnknownUsage);
  for (const next of [...def.agents, ...def.targets]) enforcement(next)?.(over);
}
