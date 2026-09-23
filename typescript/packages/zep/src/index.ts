import { ZepClient, ZepError, ZepTimeoutError } from "@getzep/zep-cloud";
import type { MemoryHit, MemoryProvider, Scope, Secret } from "@threads/core";
import {
  credential,
  type Fetch,
  type ProviderError,
  sha256Hex,
} from "@threads/core/adapter";
import { z } from "zod";
import { fencedFetcher } from "./fetcher";

// zep(): a MemoryProvider over the official Zep Cloud SDK. Each scope is one
// Zep graph named by a hash of the scope; each record is one text episode whose metadata carries
// the host's binding and the record's origin. Every request goes through the fenced fetcher
// (the SDK's transport hook). Writes are unguarded: Zep has no idempotency key, so an uncertain
// write parks rather than risk a duplicate (C3).

export type ZepOptions = {
  /** Defaults to secret("ZEP_API_KEY"), resolved at setup. */
  readonly apiKey?: string | Secret;
  readonly baseUrl?: string;
  /** The HTTP fetch (a proxy, a test server); the fence wraps it either way. */
  readonly fetch?: Fetch;
};

const Meta = z.object({
  threads_tag: z.string(),
  threads_namespace: z.string().min(1),
  threads_record_id: z.string().min(1),
  threads_origin: z.enum(["user", "tool_output", "model", "host"]),
});
/** The SDK skips response validation, so the adapter parses what it reads. */
const Episode = z.object({
  uuid: z.string().min(1),
  content: z.string(),
  score: z.number().optional(),
  relevance: z.number().optional(),
  metadata: z.unknown().optional(),
});

const graphOf = (s: Scope): string =>
  `threads-${sha256Hex(`${s.tenant_id}\n${s.agent}\n${s.scope}`)}`;

function failure(error: unknown): ProviderError {
  const message = error instanceof Error ? error.message : String(error);
  if (error instanceof ZepTimeoutError) return { code: "timeout", message };
  if (
    error instanceof ZepError &&
    error.statusCode !== undefined &&
    error.statusCode < 500 &&
    error.statusCode !== 429
  )
    return { code: "invalid", message };
  return { code: "unavailable", message };
}

/** `call`, or `otherwise` when Zep answers 404 (the scope's graph doesn't exist yet). */
async function orMissing<T, U>(
  call: () => Promise<T>,
  otherwise: () => Promise<U>,
): Promise<T | U> {
  try {
    return await call();
  } catch (error) {
    if (error instanceof ZepError && error.statusCode === 404)
      return otherwise();
    throw error;
  }
}

async function attempt<T>(
  body: () => Promise<T>,
): Promise<
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly error: ProviderError }
> {
  try {
    return { ok: true, value: await body() };
  } catch (error) {
    return { ok: false, error: failure(error) };
  }
}

export function zep(options: ZepOptions = {}): MemoryProvider {
  const apiKey = credential("zep", "apiKey", options.apiKey, "ZEP_API_KEY");
  let client: ZepClient | undefined;
  const sdk = (): ZepClient => {
    client ??= new ZepClient({
      apiKey: apiKey(),
      fetcher: fencedFetcher(options.fetch ?? globalThis.fetch),
      ...(options.baseUrl === undefined ? {} : { baseUrl: options.baseUrl }),
    });
    return client;
  };
  return {
    // A missing key is a ConfigError at setup, not a failed first call.
    setup: async () => {
      apiKey();
    },
    remember: async (scope, record) =>
      attempt(async () => {
        const graphId = graphOf(scope);
        const episode = {
          graphId,
          type: "text" as const,
          data: record.text,
          metadata: {
            threads_tag: graphId,
            threads_namespace: record.binding.namespace,
            threads_record_id: record.binding.record_id,
            threads_origin: record.origin,
          },
        };
        const add = () => sdk().graph.add(episode);
        // A 404 is Zep refusing the add (no graph yet): nothing was written, so create and add.
        const added = await orMissing(add, async () => {
          await sdk().graph.create({ graphId });
          return add();
        });
        return { id: Episode.parse(added).uuid, version: "1" };
      }),
    recall: async (scope, query, { k = 5 } = {}) =>
      attempt(async () => {
        const graphId = graphOf(scope);
        const found = await orMissing(
          () =>
            sdk().graph.search({ graphId, query, scope: "episodes", limit: k }),
          async () => ({ episodes: [] }),
        );
        return (found.episodes ?? []).flatMap((raw): MemoryHit[] => {
          const e = Episode.safeParse(raw);
          const meta = Meta.safeParse(e.data?.metadata);
          if (!e.success || !meta.success || meta.data.threads_tag !== graphId)
            return [];
          return [
            {
              id: e.data.uuid,
              version: "1",
              text: e.data.content,
              score: e.data.score ?? e.data.relevance ?? 0,
              origin: meta.data.threads_origin,
              binding: {
                namespace: meta.data.threads_namespace,
                record_id: meta.data.threads_record_id,
              },
            },
          ];
        });
      }),
    forget: async (scope, id) => {
      const got = await attempt(() => sdk().graph.episode.get(id));
      if (!got.ok) return got;
      const meta = Meta.safeParse(Episode.safeParse(got.value).data?.metadata);
      if (!meta.success || meta.data.threads_tag !== graphOf(scope))
        return {
          ok: false,
          error: { code: "invalid", message: `no memory ${id} in this scope` },
        };
      return attempt(async () => {
        await sdk().graph.episode.delete(id);
      });
    },
  };
}
