import { z } from "zod";
import { EventId } from "./ids";
import { Int } from "./primitives";
import type { EnumOf, Strict } from "./zod-types";

// What Thread.usage(), cost() and cacheBreaks() return (host-api UsageTotals, Cost, CacheBreak),
// authored here so both languages take them from the exported schema.

export const UsageTotals: Strict<{
  input_tokens: typeof Int;
  output_tokens: typeof Int;
  unknown_responses: typeof Int;
}> = z
  .strictObject({
    input_tokens: Int,
    output_tokens: Int,
    unknown_responses: Int,
  })
  .meta({
    id: "UsageTotals",
    description:
      "Sums of KNOWN input_tokens and output_tokens over model responses. unknown_responses counts responses where either is null, and responses whose known counts would take a sum past 2^53-1 (they add neither). Unknown is never summed as zero.",
  });
export type UsageTotals = z.infer<typeof UsageTotals>;

/** An ISO 4217 code: what policy.currency pins and what a Cost is counted in. */
export const Currency: z.ZodString = z.string().regex(/^[A-Z]{3}$/);

export const Cost: Strict<{
  currency: typeof Currency;
  known_nanos: typeof Int;
  upper_bound_nanos: typeof Int;
  complete: z.ZodBoolean;
  bounded: z.ZodBoolean;
}> = z
  .strictObject({
    currency: Currency,
    known_nanos: Int.describe(
      "Nano-units of currency the recorded usage costs.",
    ),
    upper_bound_nanos: Int.describe(
      "known_nanos plus each unsettled attempt's declared bound.",
    ),
    complete: z
      .boolean()
      .describe("true when every attempt's cost is known exactly."),
    bounded: z
      .boolean()
      .describe(
        "false when some unknown usage has no declared bound; upper_bound_nanos is then not a bound.",
      ),
  })
  .meta({
    id: "Cost",
    description:
      "What a thread spent: known cost and a conservative upper bound.",
  });
export type Cost = z.infer<typeof Cost>;

const CACHE_BREAK_CAUSES = [
  "settings_changed",
  "compacted",
  "context_edited",
  "tools_changed",
  "tools_loaded",
  "ttl_expired",
  "unknown",
] as const;

export const CacheBreak: Strict<{
  request_event_id: typeof EventId;
  likely_cause: EnumOf<typeof CACHE_BREAK_CAUSES>;
}> = z
  .strictObject({
    request_event_id: EventId,
    likely_cause: z.enum(CACHE_BREAK_CAUSES),
  })
  .meta({
    id: "CacheBreak",
    description:
      "A turn request whose prompt-cache reads dropped sharply, with the first likely cause before it.",
  });
export type CacheBreak = z.infer<typeof CacheBreak>;
