import { z } from "zod";
import { Actor, ActorWithPrincipal, Budget } from "../common";
import { type EventSchema, event } from "../envelope";
import { CallId, EventId, OccurrenceId } from "../ids";
import { Name, NonEmpty, TimeMs } from "../primitives";
import type { Arr, EnumOf, Lit, Opt, Strict } from "../zod-types";
import {
  type TextOrContent,
  type TextOrRef,
  textOrContent,
  textOrRef,
} from "./one-of";

// Everything that enters a thread from outside the model: user input, context, channels, schedules.

const INPUT_SOURCES = [
  "api",
  "cli",
  "channel",
  "schedule",
  "parent_agent",
  "handoff",
] as const;
type UserInputShape = {
  source: EnumOf<typeof INPUT_SOURCES>;
  delivery_event_id: Opt<typeof EventId>;
  budget: Opt<typeof Budget>;
};
export const UserInputData: TextOrContent<UserInputShape> = textOrContent({
  source: z.enum(INPUT_SOURCES),
  delivery_event_id: EventId.optional(),
  budget: Budget.optional(),
});
/** Opens a turn. actor.principal is the verified sender. */
export const UserInput: EventSchema<
  "user_input",
  typeof UserInputData,
  true,
  typeof ActorWithPrincipal
> = event("user_input", true, ActorWithPrincipal, UserInputData);

const STEER_SOURCES = ["api", "cli", "channel", "parent_agent"] as const;
type SteerShape = {
  source: EnumOf<typeof STEER_SOURCES>;
  delivery_event_id: Opt<typeof EventId>;
};
export const SteerData: TextOrContent<SteerShape> = textOrContent({
  source: z.enum(STEER_SOURCES),
  delivery_event_id: EventId.optional(),
});
/** Input that arrives while a turn is open. It does not open a turn. */
export const Steer: EventSchema<
  "steer",
  typeof SteerData,
  true,
  typeof ActorWithPrincipal
> = event("steer", true, ActorWithPrincipal, SteerData);

// These sources are always untrusted reference, never instructions (framework invariant 6).
const UNTRUSTED_SOURCES = [
  "memory",
  "knowledge",
  "attachment",
  "todo",
  "agent",
  "handoff",
] as const;
const OTHER_SOURCES = [
  "skill",
  "hook",
  "recovery",
  "mode",
  "output_style",
] as const;
const TRUST_LEVELS = ["untrusted_reference", "trusted_instruction"] as const;
export const Origin: Strict<{
  id: typeof NonEmpty;
  version: Opt<z.ZodString>;
  location: Opt<z.ZodString>;
}> = z.strictObject({
  id: NonEmpty,
  version: z.string().optional(),
  location: z.string().optional(),
});
type UntrustedShape = {
  source: EnumOf<typeof UNTRUSTED_SOURCES>;
  trust: Lit<"untrusted_reference">;
  origin: typeof Origin;
};
type OtherShape = {
  source: EnumOf<typeof OTHER_SOURCES>;
  trust: EnumOf<typeof TRUST_LEVELS>;
  origin: typeof Origin;
};
export const InjectedData: z.ZodUnion<
  readonly [TextOrRef<UntrustedShape>, TextOrRef<OtherShape>]
> = z.union([
  textOrRef({
    source: z.enum(UNTRUSTED_SOURCES),
    trust: z.literal("untrusted_reference"),
    origin: Origin,
  }),
  textOrRef({
    source: z.enum(OTHER_SOURCES),
    trust: z.enum(TRUST_LEVELS),
    origin: Origin,
  }),
]);
export const Injected: EventSchema<"injected", typeof InjectedData, true> =
  event("injected", true, Actor, InjectedData);

export const HeartbeatData: Strict<{ running_call_ids: Arr<typeof CallId> }> =
  z.strictObject({
    running_call_ids: z.array(CallId),
  });
export const Heartbeat: EventSchema<"heartbeat", typeof HeartbeatData, true> =
  event("heartbeat", true, Actor, HeartbeatData);

type ChannelShape = {
  channel: typeof Name;
  installation: typeof NonEmpty;
  conversation: typeof NonEmpty;
  delivery_id: typeof NonEmpty;
  item_key: typeof NonEmpty;
};
export const ChannelDeliveryData: TextOrRef<ChannelShape> = textOrRef({
  channel: Name,
  installation: NonEmpty,
  conversation: NonEmpty,
  delivery_id: NonEmpty,
  item_key: NonEmpty,
});
export const ChannelDelivery: EventSchema<
  "channel_delivery",
  typeof ChannelDeliveryData,
  true,
  typeof ActorWithPrincipal
> = event("channel_delivery", true, ActorWithPrincipal, ChannelDeliveryData);

type OccurrenceShape = {
  schedule_id: typeof NonEmpty;
  occurrence_id: typeof OccurrenceId;
  scheduled_for: typeof TimeMs;
  timezone: typeof NonEmpty;
};
const occurrenceShape: OccurrenceShape = {
  schedule_id: NonEmpty,
  occurrence_id: OccurrenceId,
  scheduled_for: TimeMs,
  timezone: NonEmpty,
};
export const ScheduleFiredData: Strict<OccurrenceShape> =
  z.strictObject(occurrenceShape);
export const ScheduleFired: EventSchema<
  "schedule_fired",
  typeof ScheduleFiredData,
  true
> = event("schedule_fired", true, Actor, ScheduleFiredData);

const SKIP_REASONS = ["missed", "overlap"] as const;
export const ScheduleSkippedData: Strict<
  OccurrenceShape & { reason: EnumOf<typeof SKIP_REASONS> }
> = z.strictObject({ ...occurrenceShape, reason: z.enum(SKIP_REASONS) });
export const ScheduleSkipped: EventSchema<
  "schedule_skipped",
  typeof ScheduleSkippedData,
  false
> = event("schedule_skipped", false, Actor, ScheduleSkippedData);
