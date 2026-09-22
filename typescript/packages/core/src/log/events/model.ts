import { z } from "zod";
import { Actor, ArtifactRef, Usage } from "../common";
import { OutputPart } from "../content";
import { type EventSchema, event } from "../envelope";
import { EventId } from "../ids";
import { Int, NonEmpty, PosInt, Sha256, TimeMs } from "../primitives";
import type { Arr, EnumOf, Opt, Strict } from "../zod-types";

// Model attempts: request, response, recovery, abandonment and retry waits.

const PURPOSES = ["turn", "compaction"] as const;
/** One model attempt, durable before dispatch. request_ref holds the Render v1 bytes. */
export const ModelRequestData: Strict<{
  input_bound_tokens: Opt<typeof PosInt>;
  purpose: Opt<EnumOf<typeof PURPOSES>>;
  attempt: typeof PosInt;
  request_ref: typeof ArtifactRef;
  declared_prefix: Strict<{ bytes: typeof PosInt; sha256: typeof Sha256 }>;
}> = z.strictObject({
  input_bound_tokens: PosInt.optional(),
  purpose: z.enum(PURPOSES).optional(),
  attempt: PosInt,
  request_ref: ArtifactRef,
  declared_prefix: z.strictObject({ bytes: PosInt, sha256: Sha256 }),
});
export const ModelRequest: EventSchema<
  "model_request",
  typeof ModelRequestData,
  true
> = event("model_request", true, Actor, ModelRequestData);

const STOP_REASONS = [
  "end_turn",
  "tool_use",
  "max_tokens",
  "stop_sequence",
  "refusal",
  "other",
] as const;
const COMPLETENESS = ["complete", "partial"] as const;
export const Completeness: EnumOf<typeof COMPLETENESS> = z.enum(COMPLETENESS);
type ResponseShape = {
  request_event_id: typeof EventId;
  content: Arr<typeof OutputPart>;
  stop_reason: EnumOf<typeof STOP_REASONS>;
  usage: typeof Usage;
  completeness: typeof Completeness;
};
const responseShape: ResponseShape = {
  request_event_id: EventId,
  content: z.array(OutputPart),
  stop_reason: z.enum(STOP_REASONS),
  usage: Usage,
  completeness: Completeness,
};
export const ModelResponseData: Strict<ResponseShape> =
  z.strictObject(responseShape);
export const ModelResponse: EventSchema<
  "model_response",
  typeof ModelResponseData,
  true
> = event("model_response", true, Actor, ModelResponseData);

/** Found by adapter lookup after a crash instead of re-sending. Reduce and render treat it like model_response. */
export const ModelResponseRecoveredData: Strict<
  ResponseShape & { provider_request_id: typeof NonEmpty }
> = z.strictObject({ ...responseShape, provider_request_id: NonEmpty });
export const ModelResponseRecovered: EventSchema<
  "model_response_recovered",
  typeof ModelResponseRecoveredData,
  true
> = event("model_response_recovered", true, Actor, ModelResponseRecoveredData);

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
  reason: z.enum(ABANDON_REASONS),
  http_status: Int.optional(),
  retry_after_ms: Int.optional(),
  billing: z.enum(BILLING).optional(),
});
export const ModelAttemptAbandoned: EventSchema<
  "model_attempt_abandoned",
  typeof ModelAttemptAbandonedData,
  true
> = event("model_attempt_abandoned", true, Actor, ModelAttemptAbandonedData);

const RETRY_BASES = ["retry_after", "backoff"] as const;
/** Critical: recovery reads not_before, and delay_ms counts toward the wait budget. */
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
export const RetryScheduled: EventSchema<
  "retry_scheduled",
  typeof RetryScheduledData,
  true
> = event("retry_scheduled", true, Actor, RetryScheduledData);
