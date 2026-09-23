import type { z } from "zod";
import type { Budget } from "../log";
import type { Model } from "../model";
import { tokenBounds } from "../reduce/cost";
import { ConfigError } from "./errors";

// spec/schema/README.md, Budget enforcement: a limit is reserved per attempt at the attempt's
// bound, so setup refuses a limit that one of the agent's models can't bound. TS pins no
// on_unknown_usage, so there is no stop mode to allow it.

/** Throws budget_unenforceable when some model has no per-attempt bound for a set limit. */
export function checkEnforceable(
  budget: z.infer<typeof Budget> | undefined,
  models: readonly Model[],
): void {
  if (budget === undefined) return;
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
