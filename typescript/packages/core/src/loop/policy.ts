import type { z } from "zod";
import type { ContextPolicy, Policy, RetryPolicy, Threshold } from "../log";

// The pinned policy with ADR defaults for absent sections (conformance README: "absent
// sections take the ADR defaults").

type Retry = z.infer<typeof RetryPolicy>;
type Context = z.infer<typeof ContextPolicy>;

/** . */
export const RETRY_DEFAULTS: Retry = {
  max_retries: 8,
  base_delay_ms: 1000,
  max_delay_ms: 32_000,
  max_retry_after_ms: 60_000,
  max_total_wait_ms: 600_000,
  crash_resends: 2,
  fallback_after: 3,
  fallback_scope: "turn",
  heartbeat_ms: 15_000,
};

/** . */
export const CONTEXT_DEFAULTS: Context = {
  reserve_tokens: 20_000,
  cache_ttl_ms: 300_000,
  clear_results: {
    trigger: { permille: 700 },
    keep_recent: 5,
    exclude_tools: [],
  },
  spill: {
    threshold_bytes: 32_768,
    head_bytes: 2048,
    tail_bytes: 1024,
    request_budget_bytes: 204_800,
  },
  compact: {
    trigger: { permille: 850 },
    keep_tail: { tokens: 20_000 },
    max_failures: 3,
  },
  restore: {
    max_files: 5,
    file_tokens: 5000,
    skill_tokens: 5000,
    skills_total_tokens: 25_000,
  },
  max_output_continuations: 3,
  defer_tools: "auto",
  defer_threshold: { permille: 100 },
  server_edits: "disabled",
};

export function retryPolicy(policy: Policy | undefined): Retry {
  return policy?.retry ?? RETRY_DEFAULTS;
}

export function contextPolicy(policy: Policy | undefined): Context {
  return policy?.context ?? CONTEXT_DEFAULTS;
}

/** pause_turn re-requests per turn; absent means 3 (spec/schema/README.md, turn endings). */
export function maxPauseContinuations(policy: Policy | undefined): number {
  return policy?.context?.max_pause_continuations ?? 3;
}

/** A Threshold in tokens: permille of the effective window W. */
export function tokens(
  threshold: z.infer<typeof Threshold>,
  window: number,
): number {
  return "tokens" in threshold
    ? threshold.tokens
    : Math.floor((threshold.permille * window) / 1000);
}
