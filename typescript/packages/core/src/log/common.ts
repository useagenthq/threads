import { z } from "zod";
import { Int, JsonObject, Name, NonEmpty, PosInt, Sha256 } from "./primitives";
import type { EnumOf, Lit, Opt, Strict } from "./zod-types";

/** Content-addressed bytes. Durable before any event references it; readers verify sha256 and length. */
export const ArtifactRef: Strict<{
  sha256: typeof Sha256;
  bytes: typeof Int;
  media_type: z.ZodString;
}> = z
  .strictObject({
    sha256: Sha256,
    bytes: Int,
    media_type: z.string().regex(/^[a-z0-9.+-]+\/[a-z0-9.+-]+$/),
  })
  .meta({ id: "ArtifactRef" });
export type ArtifactRef = z.infer<typeof ArtifactRef>;

// The media refs narrow media_type to an enum. Every listed value matches the ArtifactRef pattern.
type MediaRef<T extends readonly string[]> = Strict<{
  sha256: typeof Sha256;
  bytes: typeof Int;
  media_type: EnumOf<T>;
}>;

const IMAGE_TYPES = [
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
] as const;
export const ImageRef: MediaRef<typeof IMAGE_TYPES> = z
  .strictObject({ sha256: Sha256, bytes: Int, media_type: z.enum(IMAGE_TYPES) })
  .meta({ id: "ImageRef" });

const DOCUMENT_TYPES = [
  "application/pdf",
  "text/plain",
  "text/markdown",
  "text/html",
  "text/csv",
  "application/json",
] as const;
export const DocumentRef: MediaRef<typeof DOCUMENT_TYPES> = z
  .strictObject({
    sha256: Sha256,
    bytes: Int,
    media_type: z.enum(DOCUMENT_TYPES),
  })
  .meta({ id: "DocumentRef" });

const AUDIO_TYPES = [
  "audio/wav",
  "audio/mpeg",
  "audio/ogg",
  "audio/webm",
  "audio/mp4",
] as const;
export const AudioRef: MediaRef<typeof AUDIO_TYPES> = z
  .strictObject({ sha256: Sha256, bytes: Int, media_type: z.enum(AUDIO_TYPES) })
  .meta({ id: "AudioRef" });

/** A verified identity, normalized by the host. */
export const Principal: Strict<{
  issuer: typeof NonEmpty;
  tenant: typeof NonEmpty;
  subject: typeof NonEmpty;
}> = z
  .strictObject({ issuer: NonEmpty, tenant: NonEmpty, subject: NonEmpty })
  .meta({ id: "Principal" });
export type Principal = z.infer<typeof Principal>;

const ACTOR_KINDS = [
  "user",
  "model",
  "host",
  "tool",
  "channel",
  "scheduler",
  "approver",
  "recovery",
] as const;
export const ActorKind: EnumOf<typeof ACTOR_KINDS> = z.enum(ACTOR_KINDS);
export const Actor: Strict<{
  kind: typeof ActorKind;
  principal: Opt<typeof Principal>;
}> = z
  .strictObject({ kind: ActorKind, principal: Principal.optional() })
  .meta({ id: "Actor" });
export type Actor = z.infer<typeof Actor>;
export const ActorWithPrincipal: Strict<{
  kind: typeof ActorKind;
  principal: typeof Principal;
}> = z
  .strictObject({ kind: ActorKind, principal: Principal })
  .meta({ id: "ActorWithPrincipal" });
export type ActorWithPrincipal = z.infer<typeof ActorWithPrincipal>;

export const ModelRef: Strict<{
  provider: typeof Name;
  name: typeof NonEmpty;
}> = z
  .strictObject({ provider: Name, name: NonEmpty })
  .meta({ id: "ModelRef" });

/** The model adapter and every provider-conversion setting. Part of Render v1 line 0. */
export const AdapterRef: Strict<{
  name: typeof Name;
  version: typeof NonEmpty;
  settings: typeof JsonObject;
}> = z
  .strictObject({ name: Name, version: NonEmpty, settings: JsonObject })
  .meta({ id: "AdapterRef" });

const EFFECT_CLASSES = [
  "read_only",
  "sandbox_local",
  "idempotent",
  "reconcilable",
  "unguarded",
] as const;
export const EffectClass: EnumOf<typeof EFFECT_CLASSES> = z
  .enum(EFFECT_CLASSES)
  .meta({ id: "EffectClass" });

type ToolSpecBase = {
  name: typeof Name;
  description: z.ZodString;
  input_schema: typeof JsonObject;
  defer_loading: Opt<z.ZodBoolean>;
  output_schema: Opt<typeof JsonObject>;
  ends_turn: Opt<z.ZodBoolean>;
};
const toolSpecBase: ToolSpecBase = {
  name: Name,
  description: z.string(),
  input_schema: JsonObject,
  defer_loading: z.boolean().optional(),
  output_schema: JsonObject.optional(),
  ends_turn: z.boolean().optional(),
};
const NOT_IDEMPOTENT = [
  "read_only",
  "sandbox_local",
  "reconcilable",
  "unguarded",
] as const;
// dedup_window_ms is required exactly when effect_class is idempotent.
export const ToolSpec: z.ZodUnion<
  readonly [
    Strict<
      ToolSpecBase & {
        effect_class: Lit<"idempotent">;
        dedup_window_ms: typeof PosInt;
      }
    >,
    Strict<ToolSpecBase & { effect_class: EnumOf<typeof NOT_IDEMPOTENT> }>,
  ]
> = z
  .union([
    z.strictObject({
      ...toolSpecBase,
      effect_class: z.literal("idempotent"),
      dedup_window_ms: PosInt,
    }),
    z.strictObject({ ...toolSpecBase, effect_class: z.enum(NOT_IDEMPOTENT) }),
  ])
  .meta({ id: "ToolSpec" });
export type ToolSpec = z.infer<typeof ToolSpec>;

/** A measured token count, or null when the provider did not report it (unknown, never zero). */
export const Tokens: z.ZodNullable<typeof Int> = Int.nullable().meta({
  id: "Tokens",
});
export const Usage: Strict<{
  input_tokens: typeof Tokens;
  output_tokens: typeof Tokens;
  cache_read_tokens: Opt<typeof Tokens>;
  cache_write_tokens: Opt<typeof Tokens>;
  reasoning_tokens: Opt<typeof Tokens>;
}> = z
  .strictObject({
    input_tokens: Tokens,
    output_tokens: Tokens,
    cache_read_tokens: Tokens.optional(),
    cache_write_tokens: Tokens.optional(),
    reasoning_tokens: Tokens.optional(),
  })
  .meta({ id: "Usage" });
export type Usage = z.infer<typeof Usage>;

/** Half-open [start, end) in UTF-8 byte offsets. */
export const Span: Strict<{ start: typeof Int; end: typeof Int }> = z
  .strictObject({ start: Int, end: Int })
  .meta({ id: "Span" });

const PARK_KINDS = ["approval", "effect", "input", "resource"] as const;
export const ParkAddress: Strict<{
  kind: EnumOf<typeof PARK_KINDS>;
  id: typeof NonEmpty;
}> = z
  .strictObject({ kind: z.enum(PARK_KINDS), id: NonEmpty })
  .meta({ id: "ParkAddress" });

const PERMISSION_MODES = [
  "plan",
  "dont_ask",
  "default",
  "accept_edits",
  "bypass",
] as const;
export const PermissionMode: EnumOf<typeof PERMISSION_MODES> = z
  .enum(PERMISSION_MODES)
  .meta({ id: "PermissionMode" });

/** Tool(specifier) grammar, . The tool part may end in `*`. */
export const PermissionRule: z.ZodString = z
  .string()
  .regex(/^[a-z][a-z0-9_]{0,127}\*?(\(.+\))?$/)
  .meta({ id: "PermissionRule" });

type BudgetShape = {
  max_cost_nanos: Opt<typeof PosInt>;
  max_input_tokens: Opt<typeof PosInt>;
  max_output_tokens: Opt<typeof PosInt>;
  max_model_requests: Opt<typeof PosInt>;
  max_turns: Opt<typeof PosInt>;
  max_wall_ms: Opt<typeof PosInt>;
};
/** Limits checked before every model_request. */
export const Budget: Strict<BudgetShape> = z
  .strictObject({
    max_cost_nanos: PosInt.optional(),
    max_input_tokens: PosInt.optional(),
    max_output_tokens: PosInt.optional(),
    max_model_requests: PosInt.optional(),
    max_turns: PosInt.optional(),
    max_wall_ms: PosInt.optional(),
  })
  // Zod has no minProperties. The check and the exported keyword say the same thing.
  .refine(
    (budget) => Object.keys(budget).length > 0,
    "a budget sets at least one limit",
  )
  .meta({ id: "Budget", minProperties: 1 });

const CARRYOVER = ["keep", "omit_prior"] as const;
/** One settings epoch: everything in Render v1 line 0 except system and tools. */
export const ModelSettings: Strict<{
  model: typeof ModelRef;
  model_params: typeof JsonObject;
  adapter: typeof AdapterRef;
  reasoning_carryover: EnumOf<typeof CARRYOVER>;
}> = z
  .strictObject({
    model: ModelRef,
    model_params: JsonObject,
    adapter: AdapterRef,
    reasoning_carryover: z.enum(CARRYOVER),
  })
  .meta({ id: "ModelSettings" });
