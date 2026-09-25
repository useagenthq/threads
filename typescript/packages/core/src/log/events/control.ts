import { z } from "zod";
import { ActorWithPrincipal, ParkAddress } from "../common";
import { type EventDef, event, eventWithActor } from "../envelope";
import { EventId, ThreadId } from "../ids";
import { Int, TimeMs } from "../primitives";
import { type Ruled, withRule } from "../rules";
import type { EnumOf, Opt, Strict } from "../zod-types";

// Parking, cancellation, turn ends and budget stops.

const PARK_REASONS = [
  "awaiting_approval",
  "effect_unknown",
  "awaiting_input",
  "awaiting_resource",
  "awaiting_member",
] as const;
const CANCEL_SCOPES = ["turn", "thread", "tree"] as const;
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
const BUDGET_SCOPES = ["run", "thread", "ancestor"] as const;
const BUDGET_LIMITS = [
  "max_cost_nanos",
  "max_input_tokens",
  "max_output_tokens",
  "max_model_requests",
  "max_turns",
  "max_wall_ms",
] as const;

export const ParkReason: EnumOf<typeof PARK_REASONS> = z
  .enum(PARK_REASONS)
  .meta({ id: "ParkReason", description: "Why a branch is parked." });

export const ParkedData: Strict<{
  address: typeof ParkAddress;
  reason: EnumOf<typeof PARK_REASONS>;
  expires_at: Opt<typeof TimeMs>;
}> = z.strictObject({
  address: ParkAddress,
  reason: ParkReason.describe(
    "awaiting_input: an ask_user question; the address is {kind: input, id: <call_id>}. awaiting_member: an open ask ({kind: ask}), a wait ({kind: wait}) or a parked run-owned member ({kind: member}).",
  ),
  expires_at: TimeMs.optional(),
});
export const Parked: EventDef<"parked", typeof ParkedData, true> = event({
  type: "parked",
  critical: true,
  data: ParkedData,
});

export const ParkEscalatedData: Strict<{ address: typeof ParkAddress }> =
  z.strictObject({ address: ParkAddress });
export const ParkEscalated: EventDef<
  "park_escalated",
  typeof ParkEscalatedData,
  false
> = event({
  type: "park_escalated",
  critical: false,
  description:
    "A parked address outlived its TTL. The host notifies an approver; nothing is auto-resolved.",
  data: ParkEscalatedData,
});

export const ResumedData: Strict<{
  address: typeof ParkAddress;
  cause_event_id: typeof EventId;
}> = z.strictObject({
  address: ParkAddress,
  cause_event_id: EventId.describe(
    "The event that satisfied the address (approval, effect_resolved, user_input).",
  ),
});
export const Resumed: EventDef<"resumed", typeof ResumedData, true> = event({
  type: "resumed",
  critical: true,
  data: ResumedData,
});

export const CancelRequestedData: Strict<{
  scope: EnumOf<typeof CANCEL_SCOPES>;
  reason: Opt<z.ZodString>;
}> = z.strictObject({
  scope: z.enum(CANCEL_SCOPES),
  reason: z.string().optional(),
});
export const CancelRequested: EventDef<
  "cancel_requested",
  typeof CancelRequestedData,
  true,
  typeof ActorWithPrincipal
> = eventWithActor({
  type: "cancel_requested",
  critical: true,
  description:
    "Durable cancellation barrier; also the hard-stop control event. No dispatch happens after it.",
  data: CancelRequestedData,
  actor: ActorWithPrincipal,
});

export const CancelledData: Strict<{ request_event_id: typeof EventId }> =
  z.strictObject({ request_event_id: EventId });
export const Cancelled: EventDef<"cancelled", typeof CancelledData, true> =
  event({
    type: "cancelled",
    critical: true,
    description:
      "Work stopped. Must not be written while any effect is unsettled; unsettled effects park instead.",
    data: CancelledData,
  });

export const StopWhenIdleData: Strict<{ reason: Opt<z.ZodString> }> =
  z.strictObject({ reason: z.string().optional() });
export const StopWhenIdle: EventDef<
  "stop_when_idle",
  typeof StopWhenIdleData,
  true,
  typeof ActorWithPrincipal
> = eventWithActor({
  type: "stop_when_idle",
  critical: true,
  description:
    "Reserved control event: finish in-flight work (model attempt, pending calls, deferred results), start nothing new, then stop. A hard stop is cancel_requested.",
  data: StopWhenIdleData,
  actor: ActorWithPrincipal,
});

const TURN_ERROR_CODES = [
  "content_unsupported",
  "continuation_unsupported",
  "transport_fence_unsupported",
  "secret_in_provider_output",
  "pin_unavailable",
  "pin_mismatch",
  "setup_failed",
] as const;
const TURN_COMPLETED_DATA_RULE = {
  if: { required: ["code"] },
  then: { properties: { reason: { const: "error" } } },
} as const;
export const TurnCompletedData: Ruled<
  Strict<{
    reason: EnumOf<typeof TURN_END_REASONS>;
    code: Opt<EnumOf<typeof TURN_ERROR_CODES>>;
  }>,
  typeof TURN_COMPLETED_DATA_RULE
> = withRule(
  z.strictObject({
    reason: z.enum(TURN_END_REASONS),
    code: z
      .enum(TURN_ERROR_CODES)
      .describe(
        "Only with reason error: the request needed a part the model doesn't declare (content_unsupported) or another provider's continuation (continuation_unsupported), found before any model_request; or the adapter refused the send because its transport bypasses the fence (transport_fence_unsupported, never retried); or the response held a registered secret in provider material that is replayed byte-exact and so can't be redacted (secret_in_provider_output: nothing of it is stored, never retried); or a team member's rebind failed before its first model request: its definition, a tool or a model is not registered here (pin_unavailable), the rebuilt config_hash differs (pin_mismatch), or setting it up kept failing (setup_failed).",
      )
      .optional(),
  }),
  TURN_COMPLETED_DATA_RULE,
);
export const TurnCompleted: EventDef<
  "turn_completed",
  typeof TurnCompletedData,
  true
> = event({ type: "turn_completed", critical: true, data: TurnCompletedData });

const BUDGET_EXCEEDED_DATA_RULE = {
  if: { properties: { scope: { const: "ancestor" } } },
  then: { required: ["owner_thread_id"] },
  else: { not: { required: ["owner_thread_id"] } },
} as const;
export const BudgetExceededData: Ruled<
  Strict<{
    scope: EnumOf<typeof BUDGET_SCOPES>;
    owner_thread_id: Opt<typeof ThreadId>;
    limit: EnumOf<typeof BUDGET_LIMITS>;
    limit_value: typeof Int;
    observed: typeof Int;
    observed_is_upper_bound: z.ZodBoolean;
  }>,
  typeof BUDGET_EXCEEDED_DATA_RULE
> = withRule(
  z.strictObject({
    scope: z
      .enum(BUDGET_SCOPES)
      .describe(
        "Which budget refused the reservation. run and thread: this thread's own budget (the owner is the envelope thread_id, so owner_thread_id is absent). ancestor: a budget of an ancestor thread in the agent tree, named by owner_thread_id.",
      ),
    owner_thread_id: ThreadId.describe(
      "Required exactly when scope is ancestor: the ancestor thread whose budget refused the reservation.",
    ).optional(),
    limit: z.enum(BUDGET_LIMITS),
    limit_value: Int,
    observed: Int,
    observed_is_upper_bound: z
      .boolean()
      .describe(
        "true when unknown usage was counted at its conservative upper bound.",
      ),
  }),
  BUDGET_EXCEEDED_DATA_RULE,
);
export const BudgetExceeded: EventDef<
  "budget_exceeded",
  typeof BudgetExceededData,
  true
> = event({
  type: "budget_exceeded",
  critical: true,
  description:
    "A tree-wide budget reservation before a model_request failed. observed = settled usage of the whole tree under that budget plus the refused reservation. The run ends: pending calls close, then turn_completed{budget_exhausted}; no model_request follows until a new user_input.",
  data: BudgetExceededData,
});
