// Why a continued thread's pin differs from the agent's, in words that say how to continue it.
// Two pin changes came with prompt caching (lane 10), and one with tool origins (lane 22): every
// thread started before them meets them on upgrade. A Python thread pinned before configs recorded their resolved settings can't
// continue; any other change starts a new thread.

/** What both a stored thread_started and a draft one carry, as far as this check reads. */
type Started = {
  readonly tools?: readonly { readonly origin?: unknown }[];
  readonly adapter: { readonly settings: { readonly [key: string]: unknown } };
  readonly policy?:
    | {
        readonly context?: { readonly cache_ttl_ms: number } | undefined;
        readonly permissions?: unknown;
        readonly retry?: unknown;
      }
    | undefined;
};

const DEFAULT_TTL_MS = 300_000;

/** The refusal for continuing `stored` with an agent that pins `next`. */
export function pinChange(stored: Started, next: Started): string {
  // Any pin missing a section: only an older Python release pinned one from agent().
  const p = stored.policy;
  if (
    p?.permissions === undefined ||
    p.retry === undefined ||
    p.context === undefined
  )
    return "this thread was started by an older Python release that didn't pin its default permissions, retry and context settings; its config can't be matched now, so start a new thread";
  // Checked before the caching advice: an agent with extension tools can't continue such a
  // thread whatever its caching settings.
  if (hasOrigins(next) && !hasOrigins(stored))
    return "this thread was started with another config: tool origin was added in this release; start a new thread";
  if (
    next.adapter.settings["prompt_cache"] !== undefined &&
    stored.adapter.settings["prompt_cache"] === undefined
  )
    return "this thread was started before prompt caching: pass promptCache: false to anthropic() (prompt_cache=False in Python) to continue it";
  const was = stored.policy?.context?.cache_ttl_ms ?? DEFAULT_TTL_MS;
  const now = next.policy?.context?.cache_ttl_ms ?? DEFAULT_TTL_MS;
  if (was !== now)
    return `this thread judges cache breaks by a ${was} ms cache lifetime, and the agent's models declare ${now} ms: set cache_ttl_ms in the agent's context to ${was} to continue it`;
  return "this thread was started with another config; a config change starts a new thread";
}

const hasOrigins = (s: Started): boolean =>
  (s.tools ?? []).some((t) => t.origin !== undefined);
