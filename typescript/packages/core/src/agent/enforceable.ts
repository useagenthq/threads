import type { z } from "zod";
import type { Budget, Policy } from "../log";
import type { Model } from "../model";
import { tokenBounds } from "../reduce/cost";
import { ConfigError } from "./errors";

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
