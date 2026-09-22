import { z } from "zod";
import {
  Budget,
  ModelSettings,
  PermissionMode,
  PermissionRule,
} from "./common";
import { Int, JsonObject, Name, NonEmpty, PosInt, Sha256 } from "./primitives";
import type { Arr, EnumOf, Opt, Strict } from "./zod-types";

// The resolved, secret-free runtime policy pinned by thread_started (ADRs 0019-0023).
// Each section is absent (ADR defaults apply) or complete.

/** Absolute tokens, or a fraction of the effective context window in permille (850 = 85%). */
export const Threshold: z.ZodUnion<
  readonly [Strict<{ tokens: typeof PosInt }>, Strict<{ permille: z.ZodInt }>]
> = z
  .union([
    z.strictObject({ tokens: PosInt }),
    z.strictObject({ permille: z.int().min(1).max(1000) }),
  ])
  .meta({ id: "Threshold" });

/** Nano-currency units per token. */
export const Price: Strict<{
  input: typeof Int;
  output: typeof Int;
  cache_read: Opt<typeof Int>;
  cache_write: Opt<typeof Int>;
}> = z.strictObject({
  input: Int,
  output: Int,
  cache_read: Int.optional(),
  cache_write: Int.optional(),
});

const BILLING_BOUNDS = ["context_window", "none"] as const;
export const PolicyModel: Strict<{
  provider: typeof Name;
  name: typeof NonEmpty;
  context_window: typeof PosInt;
  max_output_tokens: typeof PosInt;
  input_billing_bound: EnumOf<typeof BILLING_BOUNDS>;
  price: Opt<typeof Price>;
}> = z.strictObject({
  provider: Name,
  name: NonEmpty,
  context_window: PosInt,
  max_output_tokens: PosInt,
  input_billing_bound: z.enum(BILLING_BOUNDS),
  price: Price.optional(),
});

const FALLBACK_SCOPES = ["turn", "thread"] as const;
export const RetryPolicy: Strict<{
  max_retries: typeof Int;
  base_delay_ms: typeof PosInt;
  max_delay_ms: typeof PosInt;
  max_retry_after_ms: typeof PosInt;
  max_total_wait_ms: typeof PosInt;
  crash_resends: typeof Int;
  fallback_after: typeof PosInt;
  fallback_scope: EnumOf<typeof FALLBACK_SCOPES>;
  heartbeat_ms: typeof PosInt;
}> = z.strictObject({
  max_retries: Int,
  base_delay_ms: PosInt,
  max_delay_ms: PosInt,
  max_retry_after_ms: PosInt,
  max_total_wait_ms: PosInt,
  crash_resends: Int,
  fallback_after: PosInt,
  fallback_scope: z.enum(FALLBACK_SCOPES),
  heartbeat_ms: PosInt,
});

export const ClearResults: Strict<{
  trigger: typeof Threshold;
  keep_recent: typeof Int;
  idle_ms: Opt<typeof PosInt>;
  exclude_tools: Arr<typeof Name>;
}> = z.strictObject({
  trigger: Threshold,
  keep_recent: Int,
  idle_ms: PosInt.optional(),
  exclude_tools: z.array(Name),
});

export const SpillPolicy: Strict<{
  threshold_bytes: typeof PosInt;
  head_bytes: typeof Int;
  tail_bytes: typeof Int;
  request_budget_bytes: typeof PosInt;
}> = z.strictObject({
  threshold_bytes: PosInt,
  head_bytes: Int,
  tail_bytes: Int,
  request_budget_bytes: PosInt,
});

export const CompactPolicy: Strict<{
  trigger: typeof Threshold;
  keep_tail: typeof Threshold;
  max_failures: typeof PosInt;
}> = z.strictObject({
  trigger: Threshold,
  keep_tail: Threshold,
  max_failures: PosInt,
});

export const RestorePolicy: Strict<{
  max_files: typeof Int;
  file_tokens: typeof Int;
  skill_tokens: typeof Int;
  skills_total_tokens: typeof Int;
}> = z.strictObject({
  max_files: Int,
  file_tokens: Int,
  skill_tokens: Int,
  skills_total_tokens: Int,
});

const DEFER_TOOLS = ["auto", "always", "never"] as const;
const SERVER_EDITS = ["disabled", "exact_report"] as const;
export const ContextPolicy: Strict<{
  reserve_tokens: typeof Int;
  cache_ttl_ms: typeof PosInt;
  clear_results: typeof ClearResults;
  spill: typeof SpillPolicy;
  compact: typeof CompactPolicy;
  restore: typeof RestorePolicy;
  max_output_continuations: typeof Int;
  max_output_escalation_tokens: Opt<typeof PosInt>;
  defer_tools: EnumOf<typeof DEFER_TOOLS>;
  defer_threshold: typeof Threshold;
  server_edits: EnumOf<typeof SERVER_EDITS>;
}> = z.strictObject({
  reserve_tokens: Int,
  cache_ttl_ms: PosInt,
  clear_results: ClearResults,
  spill: SpillPolicy,
  compact: CompactPolicy,
  restore: RestorePolicy,
  max_output_continuations: Int,
  max_output_escalation_tokens: PosInt.optional(),
  defer_tools: z.enum(DEFER_TOOLS),
  defer_threshold: Threshold,
  server_edits: z.enum(SERVER_EDITS),
});

export const PermissionsPolicy: Strict<{
  mode: typeof PermissionMode;
  allow: Arr<typeof PermissionRule>;
  ask: Arr<typeof PermissionRule>;
  deny: Arr<typeof PermissionRule>;
  protected_paths: Arr<typeof NonEmpty>;
  allow_bypass: z.ZodBoolean;
  plan_exit_mode: typeof PermissionMode;
}> = z
  .strictObject({
    mode: PermissionMode,
    allow: z.array(PermissionRule),
    ask: z.array(PermissionRule),
    deny: z.array(PermissionRule),
    protected_paths: z.array(NonEmpty),
    allow_bypass: z.boolean(),
    plan_exit_mode: PermissionMode,
  })
  .meta({ id: "PermissionsPolicy" });

const OUTPUT_MODES = ["tool", "native"] as const;
/** Structured final output. schema_sha256 is the RFC 8785 hash of schema. */
export const OutputPolicy: Strict<{
  schema: typeof JsonObject;
  schema_sha256: typeof Sha256;
  mode: EnumOf<typeof OUTPUT_MODES>;
  max_retries: typeof Int;
}> = z.strictObject({
  schema: JsonObject,
  schema_sha256: Sha256,
  mode: z.enum(OUTPUT_MODES),
  max_retries: Int,
});

const ON_UNKNOWN_USAGE = ["upper_bound", "stop"] as const;
export const Policy: Strict<{
  models: Opt<Arr<typeof PolicyModel>>;
  currency: Opt<z.ZodString>;
  retry: Opt<typeof RetryPolicy>;
  fallback: Opt<Arr<typeof ModelSettings>>;
  context: Opt<typeof ContextPolicy>;
  budget: Opt<typeof Budget>;
  on_unknown_usage: Opt<EnumOf<typeof ON_UNKNOWN_USAGE>>;
  permissions: Opt<typeof PermissionsPolicy>;
  output: Opt<typeof OutputPolicy>;
  handoffs: Opt<Arr<typeof NonEmpty>>;
}> = z
  .strictObject({
    models: z.array(PolicyModel).min(1).optional(),
    currency: z
      .string()
      .regex(/^[A-Z]{3}$/)
      .optional(),
    retry: RetryPolicy.optional(),
    fallback: z.array(ModelSettings).optional(),
    context: ContextPolicy.optional(),
    budget: Budget.optional(),
    on_unknown_usage: z.enum(ON_UNKNOWN_USAGE).optional(),
    permissions: PermissionsPolicy.optional(),
    output: OutputPolicy.optional(),
    handoffs: z.array(NonEmpty).optional(),
  })
  .meta({ id: "Policy" });
export type Policy = z.infer<typeof Policy>;
