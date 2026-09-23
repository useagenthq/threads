import { z } from "zod";
import type { Secret } from "../agent/secret";
import {
  InputPart,
  type JsonObject,
  type KnownEvent,
  NonEmpty,
  Principal,
  Uuid,
} from "../log";
import type { LookupCapability, LookupResult } from "../model";
import type { Result } from "../result";

// spec/api.json ChannelAdapter: what slack(), whatsapp() and github() return.
// verify, parse, ack and render are pure; perform and lookup reach the provider through a
// transport fenced by the host (sandboxFetch inside within(), ). What an adapter
// returns is parsed by the host like any boundary value, so the shapes are schemas.

/** host-api Input: text, or ordered input parts. */
export const Input: z.ZodUnion<
  readonly [z.ZodString, z.ZodArray<typeof InputPart>]
> = z.union([z.string().min(1), z.array(InputPart).min(1)]);
export type Input = z.infer<typeof Input>;

/** A webhook as received: header names lowercased, the body's exact bytes. */
export type RawRequest = {
  readonly headers: Readonly<Record<string, string>>;
  readonly body: Uint8Array;
};

export type RawResponse = {
  readonly status: number;
  readonly headers: Readonly<Record<string, string>>;
  readonly body: Uint8Array;
};

export const VerifiedDelivery: z.ZodObject<{
  tenant: typeof NonEmpty;
  installation_id: typeof NonEmpty;
  delivery_id: typeof NonEmpty;
}> = z.strictObject({
  tenant: NonEmpty,
  installation_id: NonEmpty,
  delivery_id: NonEmpty,
});
export type VerifiedDelivery = z.infer<typeof VerifiedDelivery>;

const Item = {
  principal: Principal,
  /** Conversation key; the host maps it to a thread. */
  address: NonEmpty,
  item_key: NonEmpty,
};

/** One parsed item of a webhook batch. */
export const Inbound: z.ZodDiscriminatedUnion<
  [
    z.ZodObject<{
      kind: z.ZodLiteral<"message">;
      principal: typeof Principal;
      address: typeof NonEmpty;
      item_key: typeof NonEmpty;
      content: typeof Input;
    }>,
    z.ZodObject<{
      kind: z.ZodLiteral<"decision">;
      principal: typeof Principal;
      address: typeof NonEmpty;
      item_key: typeof NonEmpty;
      challenge_id: typeof Uuid;
      decision: z.ZodEnum<{ grant: "grant"; deny: "deny" }>;
    }>,
    z.ZodObject<{
      kind: z.ZodLiteral<"control">;
      principal: typeof Principal;
      address: typeof NonEmpty;
      item_key: typeof NonEmpty;
      command: z.ZodEnum<{
        cancel: "cancel";
        stop_when_idle: "stop_when_idle";
      }>;
    }>,
    z.ZodObject<{ kind: z.ZodLiteral<"ignore"> }>,
  ],
  "kind"
> = z.discriminatedUnion("kind", [
  z.strictObject({ kind: z.literal("message"), ...Item, content: Input }),
  z.strictObject({
    kind: z.literal("decision"),
    ...Item,
    challenge_id: Uuid,
    decision: z.enum(["grant", "deny"]),
  }),
  z.strictObject({
    kind: z.literal("control"),
    ...Item,
    command: z.enum(["cancel", "stop_when_idle"]),
  }),
  z.strictObject({ kind: z.literal("ignore") }),
]);
export type Inbound = z.infer<typeof Inbound>;

/** perform's answer. */
export const DeliveryOutcome: z.ZodDiscriminatedUnion<
  [
    z.ZodObject<{
      status: z.ZodLiteral<"sent">;
      platform_ref: typeof NonEmpty;
    }>,
    z.ZodObject<{
      status: z.ZodLiteral<"delivery_error">;
      kind: z.ZodEnum<{
        rate_limited: "rate_limited";
        transient: "transient";
        permanent: "permanent";
      }>;
      sent: z.ZodEnum<{
        definite_not_sent: "definite_not_sent";
        outcome_unknown: "outcome_unknown";
      }>;
    }>,
  ],
  "status"
> = z.discriminatedUnion("status", [
  z.strictObject({ status: z.literal("sent"), platform_ref: NonEmpty }),
  z.strictObject({
    status: z.literal("delivery_error"),
    kind: z.enum(["rate_limited", "transient", "permanent"]),
    sent: z.enum(["definite_not_sent", "outcome_unknown"]),
  }),
]);
export type DeliveryOutcome = z.infer<typeof DeliveryOutcome>;

export type ChannelCapabilities = {
  /** Delivery lookup by effect key; only final may answer not_found as final. */
  readonly lookup: LookupCapability;
  readonly buttons: boolean;
  readonly edits: boolean;
  readonly files: boolean;
  readonly direct_messages: boolean;
  /** The provider dedups a re-send under the same key inside this window. */
  readonly dedup_window_ms?: number;
  readonly delivery?: "reliable" | "best_effort";
};

export type ChannelAdapter = {
  /** The host agent key this channel routes to. */
  readonly agent: string;
  readonly capabilities: ChannelCapabilities;
  /** Provider limits (message bytes, rate). */
  readonly limits: Readonly<Record<string, number>>;
  /** Credentials perform needs, by name; the host resolves them and passes their values. */
  readonly secrets: Readonly<Record<string, Secret>>;
  readonly verify: (
    raw: RawRequest,
  ) => Result<
    VerifiedDelivery,
    { readonly code: "unverified"; readonly message: string }
  >;
  readonly parse: (
    raw: RawRequest,
  ) => Result<
    readonly Inbound[],
    { readonly code: "invalid"; readonly message: string }
  >;
  readonly ack: (raw: RawRequest) => RawResponse;
  /** Optional: a provider's GET subscription check (WhatsApp hub.challenge); pure. */
  readonly challenge?: (
    query: Readonly<Record<string, string>>,
  ) => Result<
    RawResponse,
    { readonly code: "unverified"; readonly message: string }
  >;
  /**
   * Outbound ops (adapter-defined JSON) for an event: the host renders a turn's final
   * model_response and sets each op's `address` and `installation_id` before perform.
   */
  readonly render: (event: KnownEvent) => readonly z.infer<typeof JsonObject>[];
  /** Must answer definite_not_sent only when it can prove nothing reached the provider. */
  readonly perform: (
    op: z.infer<typeof JsonObject>,
    effectKey: string,
    credentials: Readonly<Record<string, string>>,
  ) => Promise<DeliveryOutcome>;
  /**
   * found carries the platform_ref. `op` is what perform was given for this key (its recorded
   * tool_call input), since a platform lookup is scoped to its conversation.
   */
  readonly lookup: (
    effectKey: string,
    op: z.infer<typeof JsonObject>,
  ) => Promise<LookupResult<string>>;
};
