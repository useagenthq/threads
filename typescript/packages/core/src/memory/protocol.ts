import { z } from "zod";
import {
  ArtifactRef,
  type EffectClass,
  EventId,
  Int,
  NonEmpty,
  type PosInt,
  Sha256,
  Span,
  ThreadId,
} from "../log";
import type { Arr, EnumOf, Opt, Strict } from "../log/zod-types";
import type { Result } from "../result";
import type { Failure } from "../sandbox/protocol";

// The memory and knowledge provider protocols (spec/api.json, ). What a
// provider returns crosses a trust boundary, so the framework parses it with these schemas
// before any use. Scope and binding are host-issued: a provider stores the binding opaquely and
// echoes it on every hit, and the host checks it against its own table (bindings.ts).

/** spec/api.json Scope: from host config and the verified principal, never a tool argument. */
export const Scope: Strict<{
  tenant_id: typeof NonEmpty;
  agent: typeof NonEmpty;
  scope: typeof NonEmpty;
}> = z.strictObject({ tenant_id: NonEmpty, agent: NonEmpty, scope: NonEmpty });
export type Scope = z.infer<typeof Scope>;

export const Binding: Strict<{
  namespace: typeof NonEmpty;
  record_id: typeof NonEmpty;
}> = z.strictObject({ namespace: NonEmpty, record_id: NonEmpty });
export type Binding = z.infer<typeof Binding>;

const ORIGINS = ["user", "tool_output", "model", "host"] as const;
export const MemoryOrigin: EnumOf<typeof ORIGINS> = z.enum(ORIGINS);
export type MemoryOrigin = z.infer<typeof MemoryOrigin>;

export const MemoryRecord: Strict<{
  text: typeof NonEmpty;
  origin: typeof MemoryOrigin;
  provenance: Strict<{
    thread_id: typeof ThreadId;
    event_ids: Arr<typeof EventId>;
  }>;
  binding: typeof Binding;
}> = z.strictObject({
  text: NonEmpty,
  origin: MemoryOrigin,
  provenance: z.strictObject({
    thread_id: ThreadId,
    event_ids: z.array(EventId),
  }),
  binding: Binding,
});
export type MemoryRecord = z.infer<typeof MemoryRecord>;

export const RecordRef: Strict<{
  id: typeof NonEmpty;
  version: typeof NonEmpty;
}> = z.strictObject({ id: NonEmpty, version: NonEmpty });
export type RecordRef = z.infer<typeof RecordRef>;

export const MemoryHit: Strict<{
  id: typeof NonEmpty;
  version: typeof NonEmpty;
  text: z.ZodString;
  score: z.ZodNumber;
  origin: typeof MemoryOrigin;
  binding: typeof Binding;
}> = z.strictObject({
  id: NonEmpty,
  version: NonEmpty,
  text: z.string(),
  score: z.number(),
  origin: MemoryOrigin,
  binding: Binding,
});
export type MemoryHit = z.infer<typeof MemoryHit>;

export const KnowledgeSource: Strict<{
  source_id: typeof NonEmpty;
  media_type: typeof NonEmpty;
  content: z.ZodCustom<Uint8Array, Uint8Array>;
  location: Opt<z.ZodString>;
  binding: typeof Binding;
}> = z.strictObject({
  source_id: NonEmpty,
  media_type: NonEmpty,
  content: z.instanceof(Uint8Array),
  location: z.string().optional(),
  binding: Binding,
});
export type KnowledgeSource = z.infer<typeof KnowledgeSource>;

export const DocVersion: Strict<{
  doc_id: typeof NonEmpty;
  version: typeof NonEmpty;
  content_sha256: typeof Sha256;
  revision: typeof Int;
}> = z.strictObject({
  doc_id: NonEmpty,
  version: NonEmpty,
  content_sha256: Sha256,
  revision: Int,
});
export type DocVersion = z.infer<typeof DocVersion>;

export const KnowledgeHit: Strict<{
  doc_id: typeof NonEmpty;
  version: typeof NonEmpty;
  span: typeof Span;
  text: z.ZodString;
  score: z.ZodNumber;
  binding: typeof Binding;
}> = z.strictObject({
  doc_id: NonEmpty,
  version: NonEmpty,
  span: Span,
  text: z.string(),
  score: z.number(),
  binding: Binding,
});
export type KnowledgeHit = z.infer<typeof KnowledgeHit>;

export const Doc: Strict<{
  doc_id: typeof NonEmpty;
  version: typeof NonEmpty;
  media_type: typeof NonEmpty;
  content_ref: typeof ArtifactRef;
  binding: typeof Binding;
}> = z.strictObject({
  doc_id: NonEmpty,
  version: NonEmpty,
  media_type: NonEmpty,
  content_ref: ArtifactRef,
  binding: Binding,
});
export type Doc = z.infer<typeof Doc>;

/** Every provider failure is a value; none crashes a run. */
export type ProviderError = Failure<
  "unavailable" | "timeout" | "invalid" | "scope_violation"
>;
type Reply<T> = Promise<Result<T, ProviderError>>;

/** spec/api.json MemoryProvider. */
export type MemoryProvider = {
  readonly remember: (
    scope: Scope,
    record: MemoryRecord,
    key: string,
  ) => Reply<RecordRef>;
  readonly recall: (
    scope: Scope,
    query: string,
    options?: { readonly k?: number },
  ) => Reply<readonly MemoryHit[]>;
  readonly forget: (scope: Scope, id: string, key: string) => Reply<void>;
  /** Absent: unguarded. idempotent needs dedupWindowMs. */
  readonly writeEffect?: z.infer<typeof EffectClass>;
  readonly dedupWindowMs?: z.infer<typeof PosInt>;
  /** Setup checks (credentials, a fenceable transport); a throw is a ConfigError. */
  readonly setup?: () => Promise<void>;
};

export type SearchOptions = {
  readonly k?: number;
  readonly sources?: readonly string[];
  /** Search as of this revision (a pinned fork). */
  readonly asOf?: number;
};

/** spec/api.json KnowledgeProvider. */
export type KnowledgeProvider = {
  readonly ingest: (
    scope: Scope,
    source: KnowledgeSource,
    key: string,
  ) => Reply<DocVersion>;
  readonly remove: (scope: Scope, docId: string, key: string) => Reply<void>;
  readonly search: (
    scope: Scope,
    query: string,
    options?: SearchOptions,
  ) => Reply<readonly KnowledgeHit[]>;
  readonly get: (
    scope: Scope,
    docId: string,
    version: string,
  ) => Promise<Result<Doc, ProviderError | Failure<"not_found">>>;
  readonly revision: (
    scope: Scope,
  ) => Promise<Result<number, Failure<"unavailable" | "timeout">>>;
  readonly setup?: () => Promise<void>;
};
