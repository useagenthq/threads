import type { Json, JsonObject, ModelInfo, Price } from "@threads/core/adapter";
import { ConfigError } from "@threads/core/adapter";

// Prompt caching: the factory's promptCache option becomes adapter setting prompt_cache (line 0),
// and the request carries cache controls derived from that setting alone. One TTL per request,
// so every cache write is billed at the one cache_write price.

export type PromptCache = "5m" | "1h" | false;

export type Ttl = "5m" | "1h";

const TTL_MS = { "5m": 300_000, "1h": 3_600_000 } as const;
/** Cache-write price per input price, as a fraction: 1.25 for 5m, 2 for 1h. */
const WRITE_RATIO = { "5m": [5, 4], "1h": [2, 1] } as const;

/** The option, checked: a JavaScript caller may pass anything. */
export function promptCache(
  given: unknown,
  hosted: readonly JsonObject[],
): Ttl | undefined {
  if (given === undefined) return "5m";
  if (given === false) return undefined;
  if (given !== "5m" && given !== "1h")
    throw new ConfigError(
      "invalid_config",
      `anthropic promptCache must be "5m", "1h" or false, not ${JSON.stringify(given)}`,
    );
  if (given === "1h" && hosted.length > 0)
    throw new ConfigError(
      "invalid_config",
      "promptCache 1h can't be combined with hostedTools: Anthropic caches their results for 5 minutes, so writes would be billed at two rates",
    );
  return given;
}

/** cache_control anywhere in params or a hosted tool would add a second source of cache writes. */
export function refuseCacheControl(
  params: JsonObject,
  hosted: readonly JsonObject[],
): void {
  const where = mentions(params)
    ? "params"
    : hosted.some(mentions)
      ? "hostedTools"
      : undefined;
  if (where !== undefined)
    throw new ConfigError(
      "invalid_config",
      `anthropic ${where} can't set cache_control: pass promptCache`,
    );
}

function mentions(value: Json | undefined): boolean {
  if (Array.isArray(value)) return value.some(mentions);
  if (typeof value !== "object" || value === null) return false;
  return Object.entries(value).some(
    ([key, inner]) => key === "cache_control" || mentions(inner),
  );
}

/** The model's declared cache lifetime (spec/api.json ModelInfo.cache). */
export function cacheInfo(
  ttl: Ttl | undefined,
): NonNullable<ModelInfo["cache"]> {
  return ttl === undefined ? "none" : { ttl_ms: TTL_MS[ttl] };
}

/**
 * Cache prices a caching model's price leaves out, rounded up to whole nano-units: a write
 * follows the TTL, and a read is 0.1 x input, the highest read rate Anthropic documents, so a
 * cost can be overstated but never understated. Explicit prices win.
 */
export function cachePrice(
  price: Price | undefined,
  ttl: Ttl | undefined,
): Price | undefined {
  if (price === undefined || ttl === undefined) return price;
  const [num, den] = WRITE_RATIO[ttl];
  return {
    ...price,
    cache_read: price.cache_read ?? Math.ceil(price.input / 10),
    cache_write: price.cache_write ?? Math.ceil((price.input * num) / den),
  };
}

/** The wire cache control for a TTL; 5m is the provider's default, so it is left implicit. */
export function cacheControl(ttl: Ttl): { readonly [key: string]: Json } {
  return ttl === "5m" ? { type: "ephemeral" } : { type: "ephemeral", ttl };
}
