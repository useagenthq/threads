import { z } from "zod";
import { ActorWithPrincipal, ArtifactRef, Budget } from "../common";
import { InputPart } from "../content";
import { type EventDef, event, eventWithActor } from "../envelope";
import { CallId, EventId, OccurrenceId } from "../ids";
import { Name, NonEmpty, TimeMs } from "../primitives";
import { type Ruled, withRule } from "../rules";
import type { Arr, EnumOf, Opt, Strict } from "../zod-types";
import { HAS_TEXT_OR_CONTENT, HAS_TEXT_OR_REF, TextOrRef } from "./one-of";

// Everything that enters a thread from outside the model: user input, context, channels, schedules.

const INPUT_SOURCES = [
  "api",
  "cli",
  "channel",
  "schedule",
  "parent_agent",
  "handoff",
] as const;
const STEER_SOURCES = ["api", "cli", "channel", "parent_agent"] as const;
const INJECTED_SOURCES = [
  "memory",
  "knowledge",
  "skill",
  "hook",
  "attachment",
  "recovery",
  "todo",
  "mode",
  "output_style",
  "agent",
  "handoff",
] as const;
const TRUST_LEVELS = ["untrusted_reference", "trusted_instruction"] as const;
const SKIP_REASONS = ["missed", "overlap"] as const;

export const UserInputData: Ruled<
  Strict<{
    source: EnumOf<typeof INPUT_SOURCES>;
    text: Opt<z.ZodString>;
    content: Opt<Arr<typeof InputPart>>;
    delivery_event_id: Opt<typeof EventId>;
    budget: Opt<typeof Budget>;
  }>,
  typeof HAS_TEXT_OR_CONTENT
> = withRule(
  z.strictObject({
    source: z.enum(INPUT_SOURCES),
    text: z.string().optional(),
    content: z.array(InputPart).min(1).optional(),
    delivery_event_id: EventId.describe(
      "The channel_delivery or schedule_fired event this input came from.",
    ).optional(),
    budget: Budget.describe(
      "The budget of the run this input starts.",
    ).optional(),
  }),
  HAS_TEXT_OR_CONTENT,
);
export const UserInput: EventDef<
  "user_input",
  typeof UserInputData,
  true,
  typeof ActorWithPrincipal
> = eventWithActor({
  type: "user_input",
  critical: true,
  description: "Opens a turn. actor.principal is the verified sender.",
  data: UserInputData,
  actor: ActorWithPrincipal,
});

export const SteerData: Ruled<
  Strict<{
    source: EnumOf<typeof STEER_SOURCES>;
    text: Opt<z.ZodString>;
    content: Opt<Arr<typeof InputPart>>;
    delivery_event_id: Opt<typeof EventId>;
  }>,
  typeof HAS_TEXT_OR_CONTENT
> = withRule(
  z.strictObject({
    source: z.enum(STEER_SOURCES),
    text: z.string().optional(),
    content: z.array(InputPart).min(1).optional(),
    delivery_event_id: EventId.optional(),
  }),
  HAS_TEXT_OR_CONTENT,
);
export const Steer: EventDef<
  "steer",
  typeof SteerData,
  true,
  typeof ActorWithPrincipal
> = eventWithActor({
  type: "steer",
  critical: true,
  description:
    "Reserved control event: new user input that arrives while a turn is open. It does not open a turn; it renders as a user line at the next model request and may preempt an in-flight model attempt (that attempt is then model_attempt_abandoned{reason: steered}).",
  data: SteerData,
  actor: ActorWithPrincipal,
});

// Recalled context is always untrusted reference, never instructions (framework invariant 6).
const UNTRUSTED_SOURCES = {
  if: {
    properties: {
      source: {
        enum: ["memory", "knowledge", "attachment", "todo", "agent", "handoff"],
      },
    },
  },
  then: { properties: { trust: { const: "untrusted_reference" } } },
} as const;
const INJECTED_DATA_RULE: {
  readonly allOf: readonly [
    { readonly $ref: typeof TextOrRef },
    typeof UNTRUSTED_SOURCES,
  ];
} = { allOf: [{ $ref: TextOrRef }, UNTRUSTED_SOURCES] };
export const InjectedData: Ruled<
  Strict<{
    source: EnumOf<typeof INJECTED_SOURCES>;
    trust: EnumOf<typeof TRUST_LEVELS>;
    origin: Strict<{
      id: typeof NonEmpty;
      version: Opt<z.ZodString>;
      location: Opt<z.ZodString>;
    }>;
    text: Opt<z.ZodString>;
    ref: Opt<typeof ArtifactRef>;
  }>,
  typeof INJECTED_DATA_RULE
> = withRule(
  z.strictObject({
    source: z.enum(INJECTED_SOURCES),
    trust: z.enum(TRUST_LEVELS),
    origin: z.strictObject({
      id: NonEmpty,
      version: z.string().optional(),
      location: z.string().optional(),
    }),
    text: z.string().optional(),
    ref: ArtifactRef.optional(),
  }),
  INJECTED_DATA_RULE,
);
export const Injected: EventDef<"injected", typeof InjectedData, true> = event({
  type: "injected",
  critical: true,
  description:
    "Model-visible context not typed by the user. memory, knowledge, attachment, todo (the agent's own list), agent (child or teammate output) and handoff (forwarded history) are always untrusted_reference.",
  data: InjectedData,
});

export const HeartbeatData: Strict<{ running_call_ids: Arr<typeof CallId> }> =
  z.strictObject({ running_call_ids: z.array(CallId) });
export const Heartbeat: EventDef<"heartbeat", typeof HeartbeatData, true> =
  event({
    type: "heartbeat",
    critical: true,
    description:
      "Reserved control event: wakes a model turn while only non-blocking tool calls are pending. Renders as a user line listing the running calls.",
    data: HeartbeatData,
  });

export const ChannelDeliveryData: Ruled<
  Strict<{
    channel: typeof Name;
    installation: typeof NonEmpty;
    conversation: typeof NonEmpty;
    delivery_id: typeof NonEmpty;
    item_key: typeof NonEmpty;
    text: Opt<z.ZodString>;
    ref: Opt<typeof ArtifactRef>;
  }>,
  typeof HAS_TEXT_OR_REF
> = withRule(
  z.strictObject({
    channel: Name,
    installation: NonEmpty,
    conversation: NonEmpty,
    delivery_id: NonEmpty,
    item_key: NonEmpty,
    text: z.string().optional(),
    ref: ArtifactRef.optional(),
  }),
  HAS_TEXT_OR_REF,
);
export const ChannelDelivery: EventDef<
  "channel_delivery",
  typeof ChannelDeliveryData,
  true,
  typeof ActorWithPrincipal
> = eventWithActor({
  type: "channel_delivery",
  critical: true,
  description:
    "One inbound item, appended when the run consumes it from the inbox. The inbox row (unique per item_key) was durable before the webhook response. item_key is the provider's per-item id, or <delivery_id>#<index> for batches without one. actor.principal is the mapped sender.",
  data: ChannelDeliveryData,
  actor: ActorWithPrincipal,
});

export const ScheduleFiredData: Strict<{
  schedule_id: typeof NonEmpty;
  occurrence_id: typeof OccurrenceId;
  scheduled_for: typeof TimeMs;
  timezone: typeof NonEmpty;
}> = z.strictObject({
  schedule_id: NonEmpty,
  occurrence_id: OccurrenceId,
  scheduled_for: TimeMs,
  timezone: NonEmpty.describe("IANA zone; UTC when the schedule omits one."),
});
export const ScheduleFired: EventDef<
  "schedule_fired",
  typeof ScheduleFiredData,
  true
> = event({
  type: "schedule_fired",
  critical: true,
  description:
    "occurrence_id is (schedule_id, scheduled instant UTC), claimed atomically in schedule_occurrences before this event is appended.",
  data: ScheduleFiredData,
});

export const ScheduleSkippedData: Strict<{
  schedule_id: typeof NonEmpty;
  occurrence_id: typeof OccurrenceId;
  scheduled_for: typeof TimeMs;
  timezone: typeof NonEmpty;
  reason: EnumOf<typeof SKIP_REASONS>;
}> = z.strictObject({
  schedule_id: NonEmpty,
  occurrence_id: OccurrenceId,
  scheduled_for: TimeMs,
  timezone: NonEmpty,
  reason: z.enum(SKIP_REASONS),
});
export const ScheduleSkipped: EventDef<
  "schedule_skipped",
  typeof ScheduleSkippedData,
  false
> = event({
  type: "schedule_skipped",
  critical: false,
  description:
    "An occurrence that was claimed but not run. Skipping it cannot change reduce or render output.",
  data: ScheduleSkippedData,
});
