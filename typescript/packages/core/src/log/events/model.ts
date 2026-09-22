import { z } from "zod";
import { ArtifactRef, Usage } from "../common";
import { OutputPart } from "../content";
import { type EventDef, event } from "../envelope";
import { EventId } from "../ids";
import { Int, NonEmpty, PosInt, Sha256, TimeMs } from "../primitives";
import type { Arr, EnumOf, Opt, Strict } from "../zod-types";

// Model attempts: request, response, recovery, abandonment and retry waits.

const PURPOSES = ["turn", "compaction"] as const;
const STOP_REASONS = [
  "end_turn",
  "tool_use",
  "max_tokens",
  "stop_sequence",
  "refusal",
  "other",
] as const;
const COMPLETENESS = ["complete", "partial"] as const;
const PROVIDER_OUTCOMES = ["not_sent", "unknown", "failed"] as const;
const ABANDON_REASONS = [
  "crash",
  "timeout",
  "provider_error",
  "stream_broken",
  "cancelled",
  "steered",
  "rate_limited",
  "overloaded",
  "server_error",
  "prompt_too_long",
  "guardrail",
] as const;
const BILLING = ["not_billed", "unknown"] as const;
const RETRY_BASES = ["retry_after", "backoff"] as const;

export const ModelRequestData: Strict<{
  input_bound_tokens: Opt<typeof PosInt>;
  purpose: Opt<EnumOf<typeof PURPOSES>>;
  attempt: typeof PosInt;
  request_ref: typeof ArtifactRef;
  declared_prefix: Strict<{ bytes: typeof PosInt; sha256: typeof Sha256 }>;
}> = z.strictObject({
  input_bound_tokens: PosInt.describe(
    "A pre-dispatch upper bound on billed input units for this attempt, from the provider's token-counting API when the adapter supports it. Absent: the model's context_window bounds it.",
  ).optional(),
  purpose: z
    .enum(PURPOSES)
    .describe(
      "turn (absent means turn) or compaction: the summarizer side request. A compaction request and its response render nothing into later requests.",
    )
    .optional(),
  attempt: PosInt.describe(
    "1 for a fresh request; n+1 when re-sending after model_attempt_abandoned.",
  ),
  request_ref: ArtifactRef,
  declared_prefix: z.strictObject({ bytes: PosInt, sha256: Sha256 }),
});
export const ModelRequest: EventDef<
  "model_request",
  typeof ModelRequestData,
  true
> = event({
  type: "model_request",
  critical: true,
  description:
    "One model attempt, durable before dispatch (C2). request_ref holds the Render v1 bytes; req_hash is request_ref.sha256 (not stored twice) and is what the adapter guard checks. declared_prefix describes Render v1 line 0, which must be byte-equal on every request of the branch (C7).",
  data: ModelRequestData,
});

export const ModelResponseData: Strict<{
  request_event_id: typeof EventId;
  content: Arr<typeof OutputPart>;
  stop_reason: EnumOf<typeof STOP_REASONS>;
  usage: typeof Usage;
  completeness: EnumOf<typeof COMPLETENESS>;
}> = z.strictObject({
  request_event_id: EventId,
  content: z.array(OutputPart),
  stop_reason: z.enum(STOP_REASONS),
  usage: Usage,
  completeness: z.enum(COMPLETENESS),
});
export const ModelResponse: EventDef<
  "model_response",
  typeof ModelResponseData,
  true
> = event({
  type: "model_response",
  critical: true,
  description:
    "complete: the stream finished. partial: the stream broke after the listed complete parts; tool calls already recorded from it keep their ids and are not re-requested.",
  data: ModelResponseData,
});

export const ModelResponseRecoveredData: Strict<{
  request_event_id: typeof EventId;
  provider_request_id: typeof NonEmpty;
  content: Arr<typeof OutputPart>;
  stop_reason: EnumOf<typeof STOP_REASONS>;
  usage: typeof Usage;
  completeness: EnumOf<typeof COMPLETENESS>;
}> = z.strictObject({
  request_event_id: EventId,
  provider_request_id: NonEmpty,
  content: z.array(OutputPart),
  stop_reason: z.enum(STOP_REASONS),
  usage: Usage,
  completeness: z.enum(COMPLETENESS),
});
export const ModelResponseRecovered: EventDef<
  "model_response_recovered",
  typeof ModelResponseRecoveredData,
  true
> = event({
  type: "model_response_recovered",
  critical: true,
  description:
    "A response recovered after a crash by adapter lookup (by the derived client request id <branch_id>:<model_request event_id>, or the provider request id) instead of re-sending. Written only when the lookup found the response with finality; not_found that is final becomes model_attempt_abandoned{provider_outcome: not_sent}, anything else is abandoned as unknown. Reduce and render treat it exactly like model_response.",
  data: ModelResponseRecoveredData,
});

export const ModelAttemptAbandonedData: Strict<{
  request_event_id: typeof EventId;
  provider_outcome: EnumOf<typeof PROVIDER_OUTCOMES>;
  reason: EnumOf<typeof ABANDON_REASONS>;
  http_status: Opt<typeof Int>;
  retry_after_ms: Opt<typeof Int>;
  billing: Opt<EnumOf<typeof BILLING>>;
}> = z.strictObject({
  request_event_id: EventId,
  provider_outcome: z.enum(PROVIDER_OUTCOMES),
  reason: z
    .enum(ABANDON_REASONS)
    .describe(
      "rate_limited (429), overloaded (529), server_error (5xx or connection reset) and prompt_too_long are provider rejections: provider_outcome failed. guardrail: a parallel input guardrail denied.",
    ),
  http_status: Int.optional(),
  retry_after_ms: Int.optional(),
  billing: z
    .enum(BILLING)
    .describe(
      "not_billed only when provider_outcome is not_sent or the adapter's declared billing contract (in line 0 adapter settings) states this rejection class is not billed; a status code alone is not proof. Absent means unknown: the attempt is charged at its bound.",
    )
    .optional(),
});
export const ModelAttemptAbandoned: EventDef<
  "model_attempt_abandoned",
  typeof ModelAttemptAbandonedData,
  true
> = event({
  type: "model_attempt_abandoned",
  critical: true,
  description:
    "A model_request with no response. provider_outcome unknown means it may have been billed; a new attempt is a new model_request with attempt+1.",
  data: ModelAttemptAbandonedData,
});

export const RetryScheduledData: Strict<{
  request_event_id: typeof EventId;
  delay_ms: typeof Int;
  not_before: typeof TimeMs;
  basis: EnumOf<typeof RETRY_BASES>;
}> = z.strictObject({
  request_event_id: EventId,
  delay_ms: Int,
  not_before: TimeMs,
  basis: z.enum(RETRY_BASES),
});
export const RetryScheduled: EventDef<
  "retry_scheduled",
  typeof RetryScheduledData,
  true
> = event({
  type: "retry_scheduled",
  critical: true,
  description:
    "A recorded wait before re-sending after a retryable rejection. Execution reads it: recovery after a crash waits only until not_before, and delay_ms counts toward max_total_wait_ms. So it is critical. The transient stream status during the wait is not logged.",
  data: RetryScheduledData,
});
