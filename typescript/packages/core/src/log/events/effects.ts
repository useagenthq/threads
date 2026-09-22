import { z } from "zod";
import { Actor, ActorWithPrincipal, ArtifactRef } from "../common";
import { ResultPart } from "../content";
import { type EventSchema, event } from "../envelope";
import { CallId } from "../ids";
import { PosInt } from "../primitives";
import type { Arr, EnumOf, Lit, Opt, Strict } from "../zod-types";
import { Completeness } from "./model";

// Side effects (begin, commit, unknown, resolved) and tool results.

/** Durable before dispatch. From then on the effect is potentially sent until settled. */
export const EffectBeginData: Strict<{
  call_id: typeof CallId;
  attempt: typeof PosInt;
}> = z.strictObject({
  call_id: CallId,
  attempt: PosInt,
});
export const EffectBegin: EventSchema<
  "effect_begin",
  typeof EffectBeginData,
  true
> = event("effect_begin", true, Actor, EffectBeginData);

export const EffectCommitData: Strict<{
  call_id: typeof CallId;
  result_ref: typeof ArtifactRef;
  provider_receipt: Opt<z.ZodString>;
}> = z.strictObject({
  call_id: CallId,
  result_ref: ArtifactRef,
  provider_receipt: z.string().optional(),
});
export const EffectCommit: EventSchema<
  "effect_commit",
  typeof EffectCommitData,
  true
> = event("effect_commit", true, Actor, EffectCommitData);

const UNKNOWN_REASONS = [
  "crash_after_begin",
  "timeout",
  "transport_error",
] as const;
export const EffectUnknownData: Strict<{
  call_id: typeof CallId;
  reason: EnumOf<typeof UNKNOWN_REASONS>;
}> = z.strictObject({ call_id: CallId, reason: z.enum(UNKNOWN_REASONS) });
export const EffectUnknown: EventSchema<
  "effect_unknown",
  typeof EffectUnknownData,
  true
> = event("effect_unknown", true, Actor, EffectUnknownData);

// Each outcome may only be settled by the parties that can prove it (C3).
type Resolved<O extends z.core.SomeType, B extends z.core.SomeType> = Strict<{
  call_id: typeof CallId;
  outcome: O;
  by: B;
  result_ref: Opt<typeof ArtifactRef>;
}>;
const BY_SUCCESS = ["reconcile", "adapter"] as const;
const BY_RETRY = ["provider_dedup", "reconcile"] as const;
const ASSUMED = ["assume_done", "assume_not_done"] as const;
export const EffectResolvedData: z.ZodDiscriminatedUnion<
  [
    Strict<{
      call_id: typeof CallId;
      outcome: Lit<"confirmed_success">;
      by: EnumOf<typeof BY_SUCCESS>;
      result_ref: typeof ArtifactRef;
    }>,
    Resolved<Lit<"safe_to_retry">, EnumOf<typeof BY_RETRY>>,
    Resolved<Lit<"not_sent">, Lit<"adapter">>,
    Resolved<Lit<"interrupted">, Lit<"sandbox_terminated">>,
    Resolved<EnumOf<typeof ASSUMED>, Lit<"human">>,
  ],
  "outcome"
> = z.discriminatedUnion("outcome", [
  z.strictObject({
    call_id: CallId,
    outcome: z.literal("confirmed_success"),
    by: z.enum(BY_SUCCESS),
    result_ref: ArtifactRef,
  }),
  z.strictObject({
    call_id: CallId,
    outcome: z.literal("safe_to_retry"),
    by: z.enum(BY_RETRY),
    result_ref: ArtifactRef.optional(),
  }),
  z.strictObject({
    call_id: CallId,
    outcome: z.literal("not_sent"),
    by: z.literal("adapter"),
    result_ref: ArtifactRef.optional(),
  }),
  z.strictObject({
    call_id: CallId,
    outcome: z.literal("interrupted"),
    by: z.literal("sandbox_terminated"),
    result_ref: ArtifactRef.optional(),
  }),
  z.strictObject({
    call_id: CallId,
    outcome: z.enum(ASSUMED),
    by: z.literal("human"),
    result_ref: ArtifactRef.optional(),
  }),
]);
export const EffectResolved: EventSchema<
  "effect_resolved",
  typeof EffectResolvedData,
  true
> = event("effect_resolved", true, Actor, EffectResolvedData);

type ResultShape = {
  call_id: typeof CallId;
  is_error: z.ZodBoolean;
  completeness: typeof Completeness;
  preview: z.ZodString;
  content: Opt<Arr<typeof ResultPart>>;
  ref: Opt<typeof ArtifactRef>;
};
const resultShape: ResultShape = {
  call_id: CallId,
  is_error: z.boolean(),
  completeness: Completeness,
  preview: z.string(),
  content: z.array(ResultPart).min(1).optional(),
  ref: ArtifactRef.optional(),
};
const EXECUTED_ORIGINS = [
  "executed",
  "materialized_from_commit",
  "denied",
  "not_executed",
  "interrupted",
  "deferred",
] as const;
// An answered result is an ask_user answer and carries the answering principal.
export const ToolResult: z.ZodUnion<
  readonly [
    EventSchema<
      "tool_result",
      Strict<ResultShape & { origin: Lit<"answered"> }>,
      true,
      typeof ActorWithPrincipal
    >,
    EventSchema<
      "tool_result",
      Strict<ResultShape & { origin: EnumOf<typeof EXECUTED_ORIGINS> }>,
      true
    >,
  ]
> = z.union([
  event(
    "tool_result",
    true,
    ActorWithPrincipal,
    z.strictObject({ ...resultShape, origin: z.literal("answered") }),
  ),
  event(
    "tool_result",
    true,
    Actor,
    z.strictObject({ ...resultShape, origin: z.enum(EXECUTED_ORIGINS) }),
  ),
]);

/** The real result of a non-blocking call, after its deferred placeholder. */
export const ToolResultLateData: Strict<ResultShape> =
  z.strictObject(resultShape);
export const ToolResultLate: EventSchema<
  "tool_result_late",
  typeof ToolResultLateData,
  true
> = event("tool_result_late", true, Actor, ToolResultLateData);
