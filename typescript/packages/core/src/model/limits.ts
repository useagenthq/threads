import { ConfigError } from "../agent/errors";
import { type ModelCatalog, parseCatalog } from "./catalog";
import { MODEL_CATALOGS } from "./generated/catalogs";

// A model factory's limits: exact-id catalog values, overridden per field by the caller. Resolved
// once, when the factory is called; they are pinned in policy.models (config_hash) and line 0.

/**
 * The default per-request output cap. A long answer continues in a new request
 * (max_output_continuations), so no request reserves the model's whole output budget.
 */
const DEFAULT_MAX_TOKENS = 8192;

/** What a model factory takes; each overrides the catalog. */
export type LimitOptions = {
  /** The most input tokens one request may carry. Pinned as policy.models[].context_window. */
  readonly maxInputTokens?: number;
  /** The most output tokens the model can produce in one response. */
  readonly maxOutputTokens?: number;
  /** The per-request output cap, pinned as params.max_tokens. Defaults to min(8192, maxOutputTokens). */
  readonly maxTokens?: number;
};

export type ModelLimits = {
  readonly max_input_tokens: number;
  readonly max_output_tokens: number;
  readonly max_tokens: number;
};

const CATALOGS: ReadonlyMap<string, ModelCatalog> = new Map(
  MODEL_CATALOGS.map((text) => {
    const parsed = parseCatalog(text);
    // The bytes were checked in CI: a failure here is a broken build, not a user error.
    if (!parsed.ok) throw new Error(`embedded model catalog: ${parsed.error}`);
    return [parsed.value.provider, parsed.value];
  }),
);

/** `factory`'s limits for `model`, from its embedded catalog (spec/models/<factory>.v1.json). */
export function modelLimits(
  factory: string,
  model: string,
  options: LimitOptions,
): ModelLimits {
  return limitsFrom(CATALOGS.get(factory), factory, model, options);
}

/** The same, from a given catalog (none: every limit must be passed). */
export function limitsFrom(
  catalog: ModelCatalog | undefined,
  factory: string,
  model: string,
  options: LimitOptions,
): ModelLimits {
  const entry = catalog?.entries.find((e) => e.id === model);
  const withdrawal = catalog?.withdrawn.find((w) => w.id === model);
  const known = withdrawal === undefined ? entry : undefined;
  const input = options.maxInputTokens ?? known?.max_input_tokens;
  const output = options.maxOutputTokens ?? known?.max_output_tokens;
  if (input === undefined || output === undefined) {
    const missing = [
      ...(input === undefined ? ["maxInputTokens"] : []),
      ...(output === undefined ? ["maxOutputTokens"] : []),
    ].join(" and ");
    const why =
      entry === undefined || withdrawal === undefined
        ? unknown(catalog, model, missing)
        : `model "${model}" was withdrawn from the catalog (${withdrawal.reason}); pass ${missing}: ` +
          `maxInputTokens: ${withdrawal.use.max_input_tokens} and maxOutputTokens: ${withdrawal.use.max_output_tokens} are correct, ` +
          `and maxInputTokens: ${entry.max_input_tokens} and maxOutputTokens: ${entry.max_output_tokens} continue threads started with the old values`;
    throw new ConfigError("invalid_config", `${factory}: ${why}`);
  }
  const maxTokens = options.maxTokens ?? Math.min(DEFAULT_MAX_TOKENS, output);
  count(factory, "maxInputTokens", input);
  count(factory, "maxOutputTokens", output);
  count(factory, "maxTokens", maxTokens);
  if (maxTokens > output)
    throw new ConfigError(
      "invalid_config",
      `${factory}: maxTokens ${maxTokens} is above maxOutputTokens ${output}; lower it`,
    );
  return {
    max_input_tokens: input,
    max_output_tokens: output,
    max_tokens: maxTokens,
  };
}

/**
 * Lists the catalog's usable ids rather than guessing a match: a near-miss id is usually a typo
 * of one of them, and matching stays exact.
 */
function unknown(
  catalog: ModelCatalog | undefined,
  model: string,
  missing: string,
): string {
  if (catalog === undefined)
    return `no catalog lists "${model}"; pass ${missing}`;
  const withdrawn = new Set(catalog.withdrawn.map((w) => w.id));
  const listed = catalog.entries
    .map((e) => e.id)
    .filter((id) => !withdrawn.has(id));
  return `unknown model "${model}"; pass ${missing}, or use a listed id: ${listed.join(", ")}`;
}

function count(factory: string, option: string, value: number): void {
  if (!Number.isSafeInteger(value) || value < 1)
    throw new ConfigError(
      "invalid_config",
      `${factory}: ${option} must be a positive whole number of tokens, not ${value}`,
    );
}
