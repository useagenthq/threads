import type { z } from "zod";
import type { Budget } from "../log";

// One budget per simulated conversation (spec lane 32, B.5): live.budget covers every agent run
// and every simulated-user run of the case, the prefix re-drive included. Before each run the
// runner passes what is left, field by field. Only fields whose remainder is >= 1 are passed
// (Budget fields are PosInt); a field the author set whose remainder is <= 0 ends the case.

export type Spent = {
  readonly requests: number;
  readonly inputTokens: number;
  readonly outputTokens: number;
  /** turn_completed events appended during this conversation, on both threads. */
  readonly turns: number;
  /** The conservative upper bound: unknown usage counts against the budget. */
  readonly costNanos: number;
  /** One monotonic clock, started before the conversation's first run. */
  readonly wallMs: number;
};

type Limits = z.infer<typeof Budget>;

export type Remaining =
  | { readonly ok: true; readonly budget: Limits }
  | { readonly ok: false };

const left = (limit: number | undefined, used: number): number | undefined =>
  limit === undefined ? undefined : limit - used;

/** What the conversation may still spend, or exhausted when a field the author set is used up. */
export function remainingBudget(budget: Limits, spent: Spent): Remaining {
  const cost = left(budget.max_cost_nanos, spent.costNanos);
  const input = left(budget.max_input_tokens, spent.inputTokens);
  const output = left(budget.max_output_tokens, spent.outputTokens);
  const requests = left(budget.max_model_requests, spent.requests);
  const turns = left(budget.max_turns, spent.turns);
  const wall = left(budget.max_wall_ms, spent.wallMs);
  const all = [cost, input, output, requests, turns, wall];
  if (all.some((v) => v !== undefined && v < 1)) return { ok: false };
  return {
    ok: true,
    budget: {
      ...(cost === undefined ? {} : { max_cost_nanos: cost }),
      ...(input === undefined ? {} : { max_input_tokens: input }),
      ...(output === undefined ? {} : { max_output_tokens: output }),
      ...(requests === undefined ? {} : { max_model_requests: requests }),
      ...(turns === undefined ? {} : { max_turns: turns }),
      ...(wall === undefined ? {} : { max_wall_ms: wall }),
    },
  };
}
