import type { Model } from "../model";
import { ConfigError } from "./errors";

// The default context.cache_ttl_ms from the models' declared cache lifetimes (ModelInfo.cache).
// cacheBreaks() judges every response of a thread by one TTL, so the default must hold for every
// model the thread can use: the primary and its fallbacks.

/**
 * The TTL every caching model declares, or undefined when none caches. Throws invalid_config
 * when a model's lifetime is unknown or two models disagree: context.cache_ttl_ms must decide.
 */
export function agreedCacheTtl(models: readonly Model[]): number | undefined {
  let agreed: { readonly model: Model; readonly ttl: number } | undefined;
  for (const model of models) {
    const { cache } = model.info;
    if (cache === "none") continue;
    if (cache === undefined)
      throw new ConfigError(
        "invalid_config",
        `the cache lifetime of ${name(model)} is unknown: set context.cache_ttl_ms, or pass cacheTtlMs to its factory`,
      );
    if (agreed === undefined) agreed = { model, ttl: cache.ttl_ms };
    else if (agreed.ttl !== cache.ttl_ms)
      throw new ConfigError(
        "invalid_config",
        `${name(agreed.model)} caches for ${span(agreed.ttl)} but fallback ${name(model)} for ${span(cache.ttl_ms)}: set context.cache_ttl_ms`,
      );
  }
  return agreed?.ttl;
}

function name(model: Model): string {
  return `${model.info.model.provider}/${model.info.model.name}`;
}

function span(ms: number): string {
  if (ms % 3_600_000 === 0) return `${ms / 3_600_000}h`;
  if (ms % 60_000 === 0) return `${ms / 60_000}m`;
  return `${ms} ms`;
}
