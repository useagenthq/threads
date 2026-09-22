import { z } from "zod";
import { Actor, ActorWithPrincipal, ArtifactRef } from "../common";
import { ResultPart } from "../content";
import { type EventDef, event, eventWithActor } from "../envelope";
import { CallId } from "../ids";
import { PosInt } from "../primitives";
import { withRule } from "../rules";
import type { Arr, EnumOf, Opt, Strict } from "../zod-types";

// Side effects (begin, commit, unknown, resolved) and tool results.

const UNKNOWN_REASONS = [
  "crash_after_begin",
  "timeout",
  "transport_error",
] as const;
const RESOLVED_OUTCOMES = [
  "confirmed_success",
  "safe_to_retry",
  "not_sent",
  "interrupted",
  "assume_done",
  "assume_not_done",
] as const;
const RESOLVERS = [
  "provider_dedup",
  "reconcile",
  "adapter",
  "sandbox_terminated",
  "human",
] as const;
const COMPLETENESS = ["complete", "partial"] as const;
const RESULT_ORIGINS = [
  "executed",
  "materialized_from_commit",
  "denied",
  "not_executed",
  "interrupted",
  "deferred",
  "answered",
] as const;

export const EffectBeginData: Strict<{
  call_id: typeof CallId;
  attempt: typeof PosInt;
}> = z.strictObject({ call_id: CallId, attempt: PosInt });
export const EffectBegin: EventDef<
  "effect_begin",
  typeof EffectBeginData,
  true
> = event({
  type: "effect_begin",
  critical: true,
  description:
    "Durable before dispatch. From the moment dispatch is accepted the effect is potentially sent until an effect_commit or effect_resolved settles it. A re-dispatch after safe_to_retry, not_sent or assume_not_done reuses the derived effect key with attempt+1.",
  data: EffectBeginData,
});

export const EffectCommitData: Strict<{
  call_id: typeof CallId;
  result_ref: typeof ArtifactRef;
  provider_receipt: Opt<z.ZodString>;
}> = z.strictObject({
  call_id: CallId,
  result_ref: ArtifactRef,
  provider_receipt: z.string().optional(),
});
export const EffectCommit: EventDef<
  "effect_commit",
  typeof EffectCommitData,
  true
> = event({ type: "effect_commit", critical: true, data: EffectCommitData });

export const EffectUnknownData: Strict<{
  call_id: typeof CallId;
  reason: EnumOf<typeof UNKNOWN_REASONS>;
}> = z.strictObject({ call_id: CallId, reason: z.enum(UNKNOWN_REASONS) });
export const EffectUnknown: EventDef<
  "effect_unknown",
  typeof EffectUnknownData,
  true
> = event({
  type: "effect_unknown",
  critical: true,
  description:
    "The outcome of a begun attempt is uncertain. Timeouts and transport errors after dispatch land here too, never in a plain error result.",
  data: EffectUnknownData,
});

export const EffectResolvedData: Strict<{
  call_id: typeof CallId;
  outcome: EnumOf<typeof RESOLVED_OUTCOMES>;
  by: EnumOf<typeof RESOLVERS>;
  result_ref: Opt<typeof ArtifactRef>;
}> = withRule(
  z.strictObject({
    call_id: CallId,
    outcome: z.enum(RESOLVED_OUTCOMES),
    by: z.enum(RESOLVERS),
    result_ref: ArtifactRef.optional(),
  }),
  {
    allOf: [
      {
        if: { properties: { outcome: { const: "confirmed_success" } } },
        then: {
          required: ["result_ref"],
          properties: { by: { enum: ["reconcile", "adapter"] } },
        },
      },
      {
        if: { properties: { outcome: { const: "safe_to_retry" } } },
        then: { properties: { by: { enum: ["provider_dedup", "reconcile"] } } },
      },
      {
        if: { properties: { outcome: { const: "not_sent" } } },
        then: { properties: { by: { const: "adapter" } } },
      },
      {
        if: { properties: { outcome: { const: "interrupted" } } },
        then: { properties: { by: { const: "sandbox_terminated" } } },
      },
      {
        if: {
          properties: { outcome: { enum: ["assume_done", "assume_not_done"] } },
        },
        then: { properties: { by: { const: "human" } } },
      },
    ],
  },
);
export const EffectResolved: EventDef<
  "effect_resolved",
  typeof EffectResolvedData,
  true
> = event({
  type: "effect_resolved",
  critical: true,
  description:
    "Settles an unknown effect (C3). confirmed_success: the effect happened (final lookup or provider receipt). safe_to_retry: a re-send under the same key is deduplicated by the provider inside its window (by provider_dedup), or a lookup whose contract is final proved absence (by reconcile). not_sent: the adapter proves the request never left, or the provider confirmed cancellation with finality. interrupted: a sandbox_local process group was confirmed terminated. assume_*: an authorized human decision. An effect that stays unknown is parked, not resolved.",
  data: EffectResolvedData,
});

export const ToolResultData: Strict<{
  call_id: typeof CallId;
  is_error: z.ZodBoolean;
  completeness: EnumOf<typeof COMPLETENESS>;
  preview: z.ZodString;
  content: Opt<Arr<typeof ResultPart>>;
  ref: Opt<typeof ArtifactRef>;
  origin: EnumOf<typeof RESULT_ORIGINS>;
}> = z.strictObject({
  call_id: CallId,
  is_error: z.boolean(),
  completeness: z.enum(COMPLETENESS),
  preview: z.string(),
  content: z.array(ResultPart).min(1).optional(),
  ref: ArtifactRef.optional(),
  origin: z.enum(RESULT_ORIGINS),
});
export const ToolResult: EventDef<
  "tool_result",
  typeof ToolResultData,
  true,
  typeof Actor
> = eventWithActor({
  type: "tool_result",
  critical: true,
  description:
    "Without content, preview is exactly what the model sees (one text part). With content, the model sees exactly those ordered parts and preview is a plain-text rendering for logs and channels. ref holds the full output when it exceeds the spill threshold. origin deferred is a placeholder for a non-blocking call whose real result arrives later as tool_result_late. origin answered is an ask_user answer and carries the answering principal.",
  data: ToolResultData,
  actor: Actor,
  rule: {
    if: {
      properties: { data: { properties: { origin: { const: "answered" } } } },
    },
    then: { properties: { actor: { $ref: ActorWithPrincipal } } },
  },
});

export const ToolResultLateData: Strict<{
  call_id: typeof CallId;
  is_error: z.ZodBoolean;
  completeness: EnumOf<typeof COMPLETENESS>;
  preview: z.ZodString;
  content: Opt<Arr<typeof ResultPart>>;
  ref: Opt<typeof ArtifactRef>;
}> = z.strictObject({
  call_id: CallId,
  is_error: z.boolean(),
  completeness: z.enum(COMPLETENESS),
  preview: z.string(),
  content: z.array(ResultPart).min(1).optional(),
  ref: ArtifactRef.optional(),
});
export const ToolResultLate: EventDef<
  "tool_result_late",
  typeof ToolResultLateData,
  true
> = event({
  type: "tool_result_late",
  critical: true,
  description:
    "Reserved: the real result of a non-blocking call, appended after its deferred placeholder and possibly after later turns. History is never rewritten; the placeholder stays.",
  data: ToolResultLateData,
});
