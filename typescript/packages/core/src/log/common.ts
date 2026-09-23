import { z } from "zod";
import { Int, JsonObject, Name, NonEmpty, PosInt, Sha256 } from "./primitives";
import { type Ruled, withRule } from "./rules";
import type { EnumOf, Opt, Strict } from "./zod-types";

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
  .meta({
    id: "ArtifactRef",
    description:
      "Content-addressed bytes. The artifact is durable before any event references it; readers verify sha256 and length.",
  });
export type ArtifactRef = z.infer<typeof ArtifactRef>;

// Each media ref narrows media_type to an enum; every value matches the ArtifactRef pattern.
function mediaRef(id: string, types: readonly string[]): typeof ArtifactRef {
  return withRule(
    ArtifactRef,
    { allOf: [{ properties: { media_type: { enum: types } } }] },
    { id },
  );
}

export const ImageRef: typeof ArtifactRef = mediaRef("ImageRef", [
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
]);
export const DocumentRef: typeof ArtifactRef = mediaRef("DocumentRef", [
  "application/pdf",
  "text/plain",
  "text/markdown",
  "text/html",
  "text/csv",
  "application/json",
]);
export const AudioRef: typeof ArtifactRef = mediaRef("AudioRef", [
  "audio/wav",
  "audio/mpeg",
  "audio/ogg",
  "audio/webm",
  "audio/mp4",
]);

export const Principal: Strict<{
  issuer: typeof NonEmpty;
  tenant: typeof NonEmpty;
  subject: typeof NonEmpty;
}> = z
  .strictObject({ issuer: NonEmpty, tenant: NonEmpty, subject: NonEmpty })
  .meta({
    id: "Principal",
    description:
      "A verified identity, normalized by the host. issuer names the identity authority (e.g. slack:T024BE7LD, api), tenant the isolation scope.",
  });
export type Principal = z.infer<typeof Principal>;

/** The normalized `issuer/tenant/subject` of a principal; `/` and `%` escaped. */
export type PrincipalKey = string & z.core.$brand<"PrincipalKey">;

export function principalKey(p: Principal): PrincipalKey {
  const part = (s: string): string =>
    s.replaceAll("%", "%25").replaceAll("/", "%2F");
  return z
    .string()
    .brand<"PrincipalKey">()
    .parse(`${part(p.issuer)}/${part(p.tenant)}/${part(p.subject)}`);
}

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
export const Actor: Strict<{
  kind: EnumOf<typeof ACTOR_KINDS>;
  principal: Opt<typeof Principal>;
}> = z
  .strictObject({ kind: z.enum(ACTOR_KINDS), principal: Principal.optional() })
  .meta({ id: "Actor" });
export type Actor = z.infer<typeof Actor>;
const ACTOR_WITH_PRINCIPAL_RULE = { required: ["kind", "principal"] } as const;
export const ActorWithPrincipal: Ruled<
  typeof Actor,
  typeof ACTOR_WITH_PRINCIPAL_RULE
> = withRule(Actor, ACTOR_WITH_PRINCIPAL_RULE, { id: "ActorWithPrincipal" });

export const ModelRef: Strict<{
  provider: typeof Name;
  name: typeof NonEmpty;
}> = z
  .strictObject({ provider: Name, name: NonEmpty })
  .meta({ id: "ModelRef" });

export const AdapterRef: Strict<{
  name: typeof Name;
  version: typeof NonEmpty;
  settings: typeof JsonObject;
}> = z
  .strictObject({ name: Name, version: NonEmpty, settings: JsonObject })
  .meta({
    id: "AdapterRef",
    description:
      "The model adapter and every provider-conversion setting that affects the wire request (API flavour, cache-breakpoint placement, tool-choice mapping). Part of Render v1 line 0.",
  });

const EFFECT_CLASSES = [
  "read_only",
  "sandbox_local",
  "idempotent",
  "reconcilable",
  "unguarded",
] as const;
export const EffectClass: EnumOf<typeof EFFECT_CLASSES> = z
  .enum(EFFECT_CLASSES)
  .meta({
    id: "EffectClass",
    description:
      "read_only: no state change anywhere; no effect events. sandbox_local: changes only sandbox state; valid only under deny-all egress or enforced operation mediation. idempotent: the provider dedups on the effect key within dedup_window_ms. reconcilable: the adapter can look up the outcome with finality. unguarded: no contract; uncertainty always parks. Undeclared tools are unguarded.",
  });

const TOOL_SPEC_RULE = {
  if: { properties: { effect_class: { const: "idempotent" } } },
  then: { required: ["dedup_window_ms"] },
  else: { not: { required: ["dedup_window_ms"] } },
} as const;
export const ToolSpec: Ruled<
  Strict<{
    name: typeof Name;
    description: z.ZodString;
    input_schema: typeof JsonObject;
    effect_class: typeof EffectClass;
    dedup_window_ms: Opt<typeof PosInt>;
    defer_loading: Opt<z.ZodBoolean>;
    output_schema: Opt<typeof JsonObject>;
    ends_turn: Opt<z.ZodBoolean>;
  }>,
  typeof TOOL_SPEC_RULE
> = withRule(
  z.strictObject({
    name: Name,
    description: z.string(),
    input_schema: JsonObject,
    effect_class: EffectClass,
    dedup_window_ms: PosInt.describe(
      "The provider's declared dedup window for this tool's effect key.",
    ).optional(),
    defer_loading: z
      .boolean()
      .describe(
        "true: deferred. Render v1 shows only {name, description, deferred: true} until a tools_changed with cause tool_search loads it (the loaded spec omits this flag). A call to a deferred tool fails pre-effect with tool_not_loaded.",
      )
      .optional(),
    output_schema: JsonObject.describe(
      "Optional JSON Schema for the tool's result value. A result that fails it is recorded as an error result; the effect still happened.",
    ).optional(),
    ends_turn: z
      .boolean()
      .describe(
        "A successful result ends the turn without another model call (the final_output tool, ).",
      )
      .optional(),
  }),
  TOOL_SPEC_RULE,
  { id: "ToolSpec" },
);
export type ToolSpec = z.infer<typeof ToolSpec>;

export const Tokens: z.ZodXor<readonly [typeof Int, z.ZodNull]> = z
  .xor([Int, z.null()])
  .meta({
    id: "Tokens",
    description:
      "A measured token count, or null when the provider did not report it (unknown, never zero). .",
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
  .meta({
    id: "Usage",
    description:
      "Per-field measured-or-unknown usage. input_tokens and output_tokens are always present and null when unknown (a stream that broke before its usage report). input_tokens excludes cache reads and writes; output_tokens includes reasoning tokens. A cache field is absent when the adapter's usage profile has no such billing category, and null when it has one but the value did not arrive.",
  });
export type Usage = z.infer<typeof Usage>;

export const Span: Strict<{ start: typeof Int; end: typeof Int }> = z
  .strictObject({ start: Int, end: Int })
  .meta({
    id: "Span",
    description:
      "Half-open range [start, end) in UTF-8 BYTE offsets of the referenced text or artifact bytes. Both ends fall on character boundaries. Python and TypeScript convert from their native string indices; a span never means code units.",
  });

const PARK_KINDS = ["approval", "effect", "input", "resource"] as const;
export const ParkAddress: Strict<{
  kind: EnumOf<typeof PARK_KINDS>;
  id: typeof NonEmpty;
}> = z.strictObject({ kind: z.enum(PARK_KINDS), id: NonEmpty }).meta({
  id: "ParkAddress",
  description:
    "What a parked branch waits on; resumed must name the same address. For kind effect, id is the derived effect key.",
});

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

export const PermissionRule: z.ZodString = z
  .string()
  .regex(/^[a-z][a-z0-9_]{0,127}\*?(\(.+\))?$/)
  .meta({
    id: "PermissionRule",
    description:
      "Tool(specifier) grammar, . The tool part may end in * (mcp__github__*).",
  });

const BUDGET_RULE = { minProperties: 1 } as const;
export const Budget: Ruled<
  Strict<{
    max_cost_nanos: Opt<typeof PosInt>;
    max_input_tokens: Opt<typeof PosInt>;
    max_output_tokens: Opt<typeof PosInt>;
    max_model_requests: Opt<typeof PosInt>;
    max_turns: Opt<typeof PosInt>;
    max_wall_ms: Opt<typeof PosInt>;
  }>,
  typeof BUDGET_RULE
> = withRule(
  z.strictObject({
    max_cost_nanos: PosInt.optional(),
    max_input_tokens: PosInt.optional(),
    max_output_tokens: PosInt.optional(),
    max_model_requests: PosInt.optional(),
    max_turns: PosInt.optional(),
    max_wall_ms: PosInt.optional(),
  }),
  BUDGET_RULE,
  {
    id: "Budget",
    description:
      "Limits checked before every model_request. Unknown usage counts at its conservative upper bound.",
  },
);

const CARRYOVER = ["keep", "omit_prior"] as const;
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
    reasoning_carryover: z
      .enum(CARRYOVER)
      .describe(
        "keep: earlier reasoning and hosted_tool parts render as recorded. omit_prior: parts recorded before this epoch are omitted from later renders (a recorded decision, not a silent drop).",
      ),
  })
  .meta({
    id: "ModelSettings",
    description:
      "One settings epoch: everything in Render v1 line 0 except system and tools.",
  });
