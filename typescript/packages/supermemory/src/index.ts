import type { MemoryHit, MemoryProvider, Scope, Secret } from "@threads/core";
import {
  credential,
  type Fetch,
  type ProviderError,
  sandboxFetch,
  sha256Hex,
} from "@threads/core/adapter";
import Supermemory, {
  APIConnectionError,
  APIConnectionTimeoutError,
  APIError,
} from "supermemory";
import { z } from "zod";

// supermemory(): a MemoryProvider over the official Supermemory SDK. Each
// record is one document in a container tag derived from the scope; the host's binding and the
// record's origin ride in its metadata, opaque to Supermemory. Every request the SDK makes goes
// through the fenced fetch, so a writer that lost its lease sends nothing. The SDK's own retries
// are off: a write is never repeated behind the loop's back (C3). Writes are unguarded:
// Supermemory documents no dedup window for customId, so an uncertain write parks.

export type SupermemoryOptions = {
  /** Defaults to secret("SUPERMEMORY_API_KEY"), resolved at setup. */
  readonly apiKey?: string | Secret;
  readonly baseUrl?: string;
  /** The SDK's fetch (a proxy, a test server); the fence wraps it either way. */
  readonly fetch?: Fetch;
};

const Meta = z.object({
  threads_tag: z.string(),
  threads_namespace: z.string().min(1),
  threads_record_id: z.string().min(1),
  threads_origin: z.enum(["user", "tool_output", "model", "host"]),
});

const Result = z.object({
  documentId: z.string().min(1),
  score: z.number(),
  content: z.string().nullish(),
  chunks: z.array(z.object({ content: z.string() })),
  metadata: z.unknown().optional(),
});

/** One container per scope; the tag is a hash, so it carries no readable label. */
const tagOf = (s: Scope): string =>
  `threads-${sha256Hex(`${s.tenant_id}\n${s.agent}\n${s.scope}`)}`;

/** An SDK failure as a typed provider error; nothing throws out of the provider. */
function failure(error: unknown): ProviderError {
  const message = error instanceof Error ? error.message : String(error);
  if (error instanceof APIConnectionTimeoutError)
    return { code: "timeout", message };
  if (error instanceof APIConnectionError)
    return { code: "unavailable", message };
  if (
    error instanceof APIError &&
    error.status !== undefined &&
    error.status < 500 &&
    error.status !== 429
  )
    return { code: "invalid", message };
  return { code: "unavailable", message };
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

export function supermemory(options: SupermemoryOptions = {}): MemoryProvider {
  const apiKey = (): string =>
    credential("supermemory", "apiKey", options.apiKey, "SUPERMEMORY_API_KEY");
  let client: Supermemory | undefined;
  const sdk = (): Supermemory => {
    client ??= new Supermemory({
      apiKey: apiKey(),
      maxRetries: 0,
      fetch: sandboxFetch(options.fetch ?? globalThis.fetch),
      ...(options.baseUrl === undefined ? {} : { baseURL: options.baseUrl }),
    });
    return client;
  };
  return {
    // A missing key is a ConfigError at setup, not a failed first call.
    setup: async () => {
      apiKey();
    },
    remember: async (scope, record, key) =>
      attempt(async () => {
        const tag = tagOf(scope);
        const added = await sdk().documents.add({
          content: record.text,
          containerTag: tag,
          customId: sha256Hex(key),
          metadata: {
            threads_tag: tag,
            threads_namespace: record.binding.namespace,
            threads_record_id: record.binding.record_id,
            threads_origin: record.origin,
          },
        });
        return { id: added.id, version: "1" };
      }),
    recall: async (scope, query, { k = 5 } = {}) =>
      attempt(async () => {
        const tag = tagOf(scope);
        const found = await sdk().search.documents({
          q: query,
          containerTags: [tag],
          limit: k,
          includeFullDocs: true,
        });
        return found.results.flatMap((raw): MemoryHit[] => {
          const r = Result.safeParse(raw);
          const meta = Meta.safeParse(r.data?.metadata);
          if (!r.success || !meta.success || meta.data.threads_tag !== tag)
            return [];
          return [
            {
              id: r.data.documentId,
              version: "1",
              text:
                r.data.content ?? r.data.chunks.map((c) => c.content).join(""),
              score: r.data.score,
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
      const tag = tagOf(scope);
      const doc = await attempt(() => sdk().documents.get(id));
      if (!doc.ok) return doc;
      const meta = Meta.safeParse(doc.value.metadata);
      if (!meta.success || meta.data.threads_tag !== tag)
        return {
          ok: false,
          error: { code: "invalid", message: `no memory ${id} in this scope` },
        };
      return attempt(async () => {
        await sdk().documents.delete(id);
      });
    },
  };
}
