import { ConfigError } from "./errors";

/**
 * spec/api.json usd: a US-dollar amount as the integer nano-dollars budgets and prices use,
 * rounded half up (usd(0.5) is 500_000_000). Throws ConfigError for a negative, non-finite or
 * too large amount.
 */
export function usd(dollars: number): number {
  const nanos = Math.floor(dollars * 1e9 + 0.5);
  if (!Number.isFinite(dollars) || dollars < 0 || !Number.isSafeInteger(nanos))
    throw new ConfigError(
      "invalid_config",
      `usd(${dollars}): give a finite, non-negative amount of at most ${Number.MAX_SAFE_INTEGER / 1e9} dollars`,
    );
  return nanos;
}
