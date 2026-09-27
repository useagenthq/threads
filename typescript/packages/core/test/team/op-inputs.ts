import { z } from "zod";
import type { EventOf } from "../../src/fold/state";
import type { TurnFailure as TurnFailureSchema } from "../../src/log";

// The inputs of the op vectors that carry a turn end (spec/conformance/vectors/team-ops.json):
// a host member's turn-only failure names its hop cap, its turn_completed and its TurnFailure.

type TurnEndData = EventOf<"turn_completed">["data"];
type HopData = Omit<
  EventOf<"budget_exceeded">["data"],
  "scope" | "observed_is_upper_bound"
>;

export const Hop: z.ZodType<HopData> = z.object({
  limit: z.enum(["max_cost_nanos", "max_input_tokens", "max_output_tokens"]),
  limit_value: z.int(),
  observed: z.int(),
});
export const TurnEnd: z.ZodType<TurnEndData> = z.object({
  reason: z.enum([
    "max_turns",
    "budget_exhausted",
    "error",
    "interrupted",
    "stop_hook_limit",
    "max_output",
    "context_exhausted",
    "output_invalid",
    "input_denied",
    "model_unavailable",
  ]),
  code: z
    .enum([
      "content_unsupported",
      "continuation_unsupported",
      "transport_fence_unsupported",
      "secret_in_provider_output",
      "pin_unavailable",
      "pin_mismatch",
      "setup_failed",
    ])
    .optional(),
});
export const Failure: z.ZodType<TurnFailureSchema> = z.object({
  code: z.enum([
    "content_unsupported",
    "continuation_unsupported",
    "transport_fence_unsupported",
    "secret_in_provider_output",
    "model_error",
    "max_turns",
    "max_output",
    "stop_hook_limit",
    "context_exhausted",
    "output_invalid",
    "input_denied",
    "model_unavailable",
    "budget_exhausted",
    "interrupted",
  ]),
  message: z.string(),
});
