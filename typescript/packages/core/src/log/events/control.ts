import { z } from "zod";
import { Actor, ActorWithPrincipal, ParkAddress } from "../common";
import { type EventSchema, event } from "../envelope";
import { EventId, ThreadId } from "../ids";
import { Int, TimeMs } from "../primitives";
import type { EnumOf, Lit, Opt, Strict } from "../zod-types";

// Parking, cancellation, turn ends and budget stops.

const PARK_REASONS = [
  "awaiting_approval",
  "effect_unknown",
  "awaiting_input",
  "awaiting_resource",
] as const;
export const ParkedData: Strict<{
  address: typeof ParkAddress;
  reason: EnumOf<typeof PARK_REASONS>;
  expires_at: Opt<typeof TimeMs>;
}> = z.strictObject({
  address: ParkAddress,
  reason: z.enum(PARK_REASONS),
  expires_at: TimeMs.optional(),
});
export const Parked: EventSchema<"parked", typeof ParkedData, true> = event(
  "parked",
  true,
  Actor,
  ParkedData,
);

/** A parked address outlived its TTL. Nothing is auto-resolved. */
export const ParkEscalatedData: Strict<{ address: typeof ParkAddress }> =
  z.strictObject({ address: ParkAddress });
export const ParkEscalated: EventSchema<
  "park_escalated",
  typeof ParkEscalatedData,
  false
> = event("park_escalated", false, Actor, ParkEscalatedData);

export const ResumedData: Strict<{
  address: typeof ParkAddress;
  cause_event_id: typeof EventId;
}> = z.strictObject({
  address: ParkAddress,
  cause_event_id: EventId,
});
export const Resumed: EventSchema<"resumed", typeof ResumedData, true> = event(
  "resumed",
  true,
  Actor,
  ResumedData,
);

const CANCEL_SCOPES = ["turn", "thread", "tree"] as const;
/** Durable cancellation barrier and the hard-stop control event. */
export const CancelRequestedData: Strict<{
  scope: EnumOf<typeof CANCEL_SCOPES>;
  reason: Opt<z.ZodString>;
}> = z.strictObject({
  scope: z.enum(CANCEL_SCOPES),
  reason: z.string().optional(),
});
export const CancelRequested: EventSchema<
  "cancel_requested",
  typeof CancelRequestedData,
  true,
  typeof ActorWithPrincipal
> = event("cancel_requested", true, ActorWithPrincipal, CancelRequestedData);

export const CancelledData: Strict<{ request_event_id: typeof EventId }> =
  z.strictObject({
    request_event_id: EventId,
  });
export const Cancelled: EventSchema<"cancelled", typeof CancelledData, true> =
  event("cancelled", true, Actor, CancelledData);

export const StopWhenIdleData: Strict<{ reason: Opt<z.ZodString> }> =
  z.strictObject({
    reason: z.string().optional(),
  });
export const StopWhenIdle: EventSchema<
  "stop_when_idle",
  typeof StopWhenIdleData,
  true,
  typeof ActorWithPrincipal
> = event("stop_when_idle", true, ActorWithPrincipal, StopWhenIdleData);

const TURN_END_REASONS = [
  "end_turn",
  "max_turns",
  "budget_exhausted",
  "cancelled",
  "error",
  "interrupted",
  "stop_hook_limit",
  "max_output",
  "context_exhausted",
  "output_invalid",
  "input_denied",
  "model_unavailable",
  "handoff",
] as const;
export const TurnCompletedData: Strict<{
  reason: EnumOf<typeof TURN_END_REASONS>;
}> = z.strictObject({
  reason: z.enum(TURN_END_REASONS),
});
export const TurnCompleted: EventSchema<
  "turn_completed",
  typeof TurnCompletedData,
  true
> = event("turn_completed", true, Actor, TurnCompletedData);

const OWN_SCOPES = ["run", "thread"] as const;
const BUDGET_LIMITS = [
  "max_cost_nanos",
  "max_input_tokens",
  "max_output_tokens",
  "max_model_requests",
  "max_turns",
  "max_wall_ms",
] as const;
type BudgetShape = {
  limit: EnumOf<typeof BUDGET_LIMITS>;
  limit_value: typeof Int;
  observed: typeof Int;
  observed_is_upper_bound: z.ZodBoolean;
};
const budgetShape: BudgetShape = {
  limit: z.enum(BUDGET_LIMITS),
  limit_value: Int,
  observed: Int,
  observed_is_upper_bound: z.boolean(),
};
/** A tree-wide budget reservation before a model_request failed. */
// An own budget's owner is the envelope thread; only an ancestor's is named.
export const BudgetExceededData: z.ZodDiscriminatedUnion<
  [
    Strict<BudgetShape & { scope: EnumOf<typeof OWN_SCOPES> }>,
    Strict<
      BudgetShape & { scope: Lit<"ancestor">; owner_thread_id: typeof ThreadId }
    >,
  ],
  "scope"
> = z.discriminatedUnion("scope", [
  z.strictObject({ ...budgetShape, scope: z.enum(OWN_SCOPES) }),
  z.strictObject({
    ...budgetShape,
    scope: z.literal("ancestor"),
    owner_thread_id: ThreadId,
  }),
]);
export const BudgetExceeded: EventSchema<
  "budget_exceeded",
  typeof BudgetExceededData,
  true
> = event("budget_exceeded", true, Actor, BudgetExceededData);
