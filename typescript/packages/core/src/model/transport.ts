import type { ModelChunk, ModelContext } from "./protocol";

// Shared by the HTTP model adapters: the fence at the real send point, and the // item 3 rejection classes.

export type Fetch = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;

/** Thrown by a fenced fetch before any byte leaves: the writer lost its lease. */
export class StaleEpochError extends Error {
  override readonly name = "StaleEpochError";
}

/** The StaleEpochError behind an SDK's wrapped transport error, if that is what stopped it. */
export function staleEpoch(error: unknown): StaleEpochError | undefined {
  for (let e = error; e instanceof Error; e = e.cause)
    if (e instanceof StaleEpochError) return e;
  return undefined;
}

/**
 * Wraps the fetch an SDK sends through, so the lease is re-checked at the moment a request
 * would leave, after any SDK queueing. A stale epoch throws before any byte.
 */
export function fencedFetch(context: ModelContext, inner: Fetch): Fetch {
  return async (input, init) => {
    const live = await context.fence();
    if (!live.ok) throw new StaleEpochError(live.error.message);
    return inner(input, init);
  };
}

type Rejected = Extract<ModelChunk, { kind: "rejected" }>;

/** `retry-after-ms`, else `retry-after` in seconds or as an HTTP date. */
export function retryAfterMs(
  headers: Headers | undefined,
  now: number,
): number | undefined {
  const ms = Number(headers?.get("retry-after-ms") ?? Number.NaN);
  if (Number.isFinite(ms) && ms >= 0) return Math.ceil(ms);
  const value = headers?.get("retry-after");
  if (value === null || value === undefined) return undefined;
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds >= 0)
    return Math.ceil(seconds * 1000);
  const date = Date.parse(value);
  return Number.isNaN(date) ? undefined : Math.max(0, date - now);
}

/**
 * A provider's HTTP failure before any response content, as a rejected chunk (item
 * 3). `retryable` is false for a 429 the provider says won't clear (an exhausted quota).
 */
export function rejectionFor(
  status: number,
  headers: Headers | undefined,
  kind: { readonly promptTooLong: boolean; readonly retryable: boolean },
  now: number,
): Rejected {
  const base = { kind: "rejected", http_status: status } as const;
  if (status === 429 && kind.retryable) {
    const wait = retryAfterMs(headers, now);
    return {
      ...base,
      reason: "rate_limited",
      ...(wait === undefined ? {} : { retry_after_ms: wait }),
    };
  }
  if (status === 529 || status === 503)
    return { ...base, reason: "overloaded" };
  if (status >= 500) return { ...base, reason: "server_error" };
  if (kind.promptTooLong) return { ...base, reason: "prompt_too_long" };
  return { ...base, reason: "provider_error" };
}
