import type { z } from "zod";
import { sha256Hex } from "../hash";
import { canonicalize, type Policy, type ToolSpec } from "../log";
import { FINAL_OUTPUT, RETRY_DEFAULTS } from "../loop";
import { CONTEXT_DEFAULTS } from "../loop/policy";
import { DEFAULT_PERMISSIONS } from "../permissions";
import { unchecked } from "../validate/json-schema";
import { agreedCacheTtl } from "./cache-ttl";
import { ConfigError } from "./errors";
import type { PinOptions } from "./pin";
import { jsonSchema } from "./tool";

// The pinned policy and output mode of an agent's config (thread_started.policy), and the checks
// on the options they come from.

/** Tool mode: final_output takes the output schema and ends the turn. */
export function finalOutput(
  output: z.ZodType | undefined,
): readonly ToolSpec[] {
  if (output === undefined) return [];
  return [
    {
      name: FINAL_OUTPUT,
      description: "Return the final structured result.",
      input_schema: jsonSchema(FINAL_OUTPUT, output),
      effect_class: "read_only",
      ends_turn: true,
    },
  ];
}

export function policy(o: PinOptions): Policy {
  const models = [o.model, ...o.fallback].map((m) => m.info.limits);
  return {
    models: models.filter(
      (m, i) =>
        models.findIndex(
          (x) => x.provider === m.provider && x.name === m.name,
        ) === i,
    ),
    // Prices are nano-USD (spec/schema/README.md); without a currency cost() would be null.
    ...(models.some((m) => m.price !== undefined) ? { currency: "USD" } : {}),
    permissions: { ...DEFAULT_PERMISSIONS, ...o.permissions },
    retry: { ...RETRY_DEFAULTS, ...o.retry },
    context: { ...CONTEXT_DEFAULTS, ...cacheTtl(o), ...o.context },
    ...(o.fallback.length === 0
      ? {}
      : {
          fallback: o.fallback.map((m) => ({
            model: m.info.model,
            model_params: m.info.params,
            adapter: m.info.adapter,
            reasoning_carryover: "keep" as const,
          })),
        }),
    ...(o.budget === undefined ? {} : { budget: o.budget }),
    ...(o.onUnknownUsage === undefined
      ? {}
      : { on_unknown_usage: o.onUnknownUsage }),
    ...(o.handoffs.length === 0 ? {} : { handoffs: [...o.handoffs] }),
    // Absent when none is defined, so agents without styles keep their config_hash.
    ...(Object.keys(o.outputStyles).length === 0
      ? {}
      : { output_styles: { ...o.outputStyles } }),
    ...(o.output === undefined
      ? {}
      : { output: outputPolicy(o.output, o.outputRetries) }),
  };
}

/** The models' agreed cache lifetime, checked only when the agent leaves cache_ttl_ms unset. */
function cacheTtl(o: PinOptions): { readonly cache_ttl_ms?: number } {
  if (o.context.cache_ttl_ms !== undefined) return {};
  const ttl = agreedCacheTtl([o.model, ...o.fallback]);
  return ttl === undefined ? {} : { cache_ttl_ms: ttl };
}

/** Every output style has a name and a text: an empty one could never be switched to. */
export function checkStyles(styles: Readonly<Record<string, unknown>>): void {
  for (const [name, text] of Object.entries(styles))
    if (name === "" || typeof text !== "string" || text === "")
      throw new ConfigError(
        "invalid_config",
        `outputStyles: style ${JSON.stringify(name)} needs a non-empty name and text`,
      );
}

/** outputRetries counts failed candidates: a non-negative integer. */
export function checkRetries(n: number): void {
  if (!Number.isSafeInteger(n) || n < 0)
    throw new ConfigError(
      "invalid_config",
      `outputRetries must be an integer from 0 to 2**53 - 1, got ${n}`,
    );
}

function outputPolicy(
  schema: z.ZodType,
  maxRetries: number,
): NonNullable<Policy["output"]> {
  const exported = jsonSchema("output", schema);
  // Refused here, never by a run that has an answer to record: the log checks every keyword.
  const why = unchecked(exported);
  if (why !== undefined)
    throw new ConfigError(
      "invalid_config",
      `output: ${why}; use a bound, a length, a pattern, an enum or a format the log checks`,
    );
  const text = canonicalize(exported);
  if (!text.ok) throw new ConfigError("invalid_config", text.error.message);
  return {
    schema: exported,
    schema_sha256: sha256Hex(text.value),
    mode: "tool",
    max_retries: maxRetries,
  };
}
