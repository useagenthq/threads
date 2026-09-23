import type { z } from "zod";
import { ConfigError } from "../agent/errors";
import { jsonSchema } from "../agent/tool";
import {
  EventId,
  type InjectedData,
  type KnownEvent,
  ThreadId,
  ToolSpec,
} from "../log";
import type { ToolImpl, ToolRun } from "../loop/types";
import type { HostBindings } from "../store/bindings";
import { type CatalogEntry, entry } from "../tools/catalog";
import {
  ForgetMemoryInput,
  SaveMemoryInput,
  SearchKnowledgeInput,
  SearchMemoryInput,
} from "../tools/memory-inputs";
import { called, failedRun, readFailure, writeRun } from "./calls";
import { knowledgeRevision, memoryOrigin } from "./context";
import {
  KnowledgeHit,
  type KnowledgeProvider,
  MemoryHit,
  type MemoryProvider,
  type Scope,
} from "./protocol";

// The memory and knowledge tools (catalog entries save_memory, search_memory,
// forget_memory, search_knowledge). They run on the host. Scope comes from the run, never from
// arguments. Every hit is parsed (a provider is a boundary), then kept only if the host issued
// its binding to this scope; a foreign hit is dropped and audited. What survives is appended as
// injected untrusted reference with the result, before the next request (items 1-3). Result
// texts match the Python host's.

/** Model-visible bytes one search may bring (item 9: bounded by k and bytes). */
const MAX_HIT_BYTES = 16_384;
const DEFAULT_K = 5;
const encoder = new TextEncoder();

type Inject = z.infer<typeof InjectedData>;

export type MemoryEnv = {
  readonly threadId: string;
  readonly scope: Scope;
  readonly bindings: HostBindings;
  readonly events: () => readonly KnownEvent[];
};

function spec(e: CatalogEntry, effect: Partial<ToolSpec>): ToolSpec {
  return ToolSpec.parse({
    name: e.name,
    description: e.description,
    input_schema: jsonSchema(e.name, e.input),
    effect_class: "read_only",
    ...effect,
  });
}

/** A memory write carries the provider's declared effect class (absent: unguarded). */
function writeSpec(name: string, provider: MemoryProvider): ToolSpec {
  // A write is always an effect with effect_begin before dispatch.
  if (provider.writeEffect === "read_only")
    throw new ConfigError(
      "invalid_config",
      "a memory provider's writes can't be read_only: save_memory and forget_memory are effects",
    );
  return spec(entry(name), {
    effect_class: provider.writeEffect ?? "unguarded",
    ...(provider.dedupWindowMs === undefined
      ? {}
      : { dedup_window_ms: provider.dedupWindowMs }),
  });
}

/** What the agent pins for a configured memory provider (sorted by name). */
export function memorySpecs(provider: MemoryProvider): readonly ToolSpec[] {
  return [
    writeSpec("forget_memory", provider),
    writeSpec("save_memory", provider),
    spec(entry("search_memory"), {}),
  ];
}

export function knowledgeSpecs(): readonly ToolSpec[] {
  return [spec(entry("search_knowledge"), {})];
}

/**
 * Hits whose binding the host issued to this scope (the rest audited as scope_violation), at
 * most k and MAX_HIT_BYTES of text: a hit that would pass the budget ends the list.
 */
function kept<
  H extends { readonly binding: MemoryHit["binding"]; readonly text: string },
>(
  env: MemoryEnv,
  kind: "memory" | "knowledge",
  hits: readonly H[],
  k: number,
): readonly H[] {
  const out: H[] = [];
  let size = 0;
  for (const h of hits) {
    if (!env.bindings.owns(kind, env.scope, h.binding)) {
      env.bindings.violation(kind, env.scope, h.binding);
      continue;
    }
    size += encoder.encode(h.text).length;
    if (out.length === k || size > MAX_HIT_BYTES) break;
    out.push(h);
  }
  return out;
}

/** `names` are listed in the bare result text, so only host-derived citation tags go there. */
function listed(
  kind: string,
  inject: readonly Inject[],
  names?: readonly string[],
): ToolRun {
  const head = `${inject.length} ${kind}, shown below as untrusted references`;
  return {
    kind: "done",
    output:
      inject.length === 0
        ? `no ${kind} found`
        : names === undefined
          ? head
          : `${head}: ${names.join(", ")}`,
    isError: false,
    inject,
  };
}

function searchMemory(provider: MemoryProvider, env: MemoryEnv): ToolImpl {
  return {
    spec: spec(entry("search_memory"), {}),
    input: SearchMemoryInput,
    run: async (raw, ctx) => {
      const input = SearchMemoryInput.parse(raw);
      const k = input.k ?? DEFAULT_K;
      const got = await called(ctx, () =>
        provider.recall(env.scope, input.query, { k }),
      );
      if (!got.ok) return readFailure(got);
      const parsed = MemoryHit.array().safeParse(got.value);
      if (!parsed.success)
        return failedRun({ code: "invalid", message: "malformed hits" });
      const hits = kept(env, "memory", parsed.data, k);
      // The provider chose each id and version: they reach the model only inside the
      // reference wrapper (invariant 6), never in this bare text.
      return listed(
        "memories",
        hits.map((h) => ({
          source: "memory",
          trust: "untrusted_reference",
          origin: { id: h.id, version: h.version },
          text: h.text,
        })),
      );
    },
  };
}

function saveMemory(provider: MemoryProvider, env: MemoryEnv): ToolImpl {
  return {
    spec: writeSpec("save_memory", provider),
    input: SaveMemoryInput,
    run: async (raw, ctx) => {
      const input = SaveMemoryInput.parse(raw);
      const events = env.events();
      // The turn's input and this call: what the record came from.
      const ids = events.flatMap((e) =>
        e.type === "user_input" ||
        (e.type === "tool_call" && e.data.call_id === ctx.callId)
          ? [EventId.parse(e.event_id)]
          : [],
      );
      const record = {
        text: input.text,
        origin: memoryOrigin(events),
        provenance: {
          thread_id: ThreadId.parse(env.threadId),
          event_ids: ids.slice(-2),
        },
        // Derived from the key, so a re-dispatch writes the same record (item 4).
        binding: env.bindings.issue("memory", env.scope, ctx.effectKey),
      };
      const got = await called(ctx, () =>
        provider.remember(env.scope, record, ctx.effectKey),
      );
      // The Python host's text: json.dumps of the ref.
      return writeRun(
        got,
        (ref) =>
          `{"id": ${JSON.stringify(ref.id)}, "version": ${JSON.stringify(ref.version)}}`,
      );
    },
  };
}

function forgetMemory(provider: MemoryProvider, env: MemoryEnv): ToolImpl {
  return {
    spec: writeSpec("forget_memory", provider),
    input: ForgetMemoryInput,
    run: async (raw, ctx) => {
      const { id } = ForgetMemoryInput.parse(raw);
      // Only an id this branch recalled, so one that passed the scope check, can be forgotten.
      const recalled = env
        .events()
        .some(
          (e) =>
            e.type === "injected" &&
            e.data.source === "memory" &&
            e.data.origin.id === id,
        );
      if (!recalled)
        return {
          kind: "done",
          output: `not_found: memory ${id} was not recalled in this thread`,
          isError: true,
        };
      const got = await called(ctx, () =>
        provider.forget(env.scope, id, ctx.effectKey),
      );
      return writeRun(got, () => `forgot ${id}`);
    },
  };
}

function searchKnowledge(
  provider: KnowledgeProvider,
  env: MemoryEnv,
): ToolImpl {
  return {
    spec: spec(entry("search_knowledge"), {}),
    input: SearchKnowledgeInput,
    run: async (raw, ctx) => {
      const input = SearchKnowledgeInput.parse(raw);
      const k = input.k ?? DEFAULT_K;
      // A pinned fork searches as of its snapshot's revision.
      const asOf = knowledgeRevision(env.events());
      const got = await called(ctx, () =>
        provider.search(env.scope, input.query, {
          k,
          ...(input.sources === undefined ? {} : { sources: input.sources }),
          ...(asOf === undefined ? {} : { asOf }),
        }),
      );
      if (!got.ok) return readFailure(got);
      const parsed = KnowledgeHit.array().safeParse(got.value);
      if (!parsed.success)
        return failedRun({ code: "invalid", message: "malformed hits" });
      const refs = kept(env, "knowledge", parsed.data, k).map(
        (h): Inject => ({
          source: "knowledge",
          trust: "untrusted_reference",
          origin: {
            id: h.doc_id,
            version: h.version,
            location: `${h.span.start}-${h.span.end}`,
          },
          text: h.text,
        }),
      );
      return listed(
        "excerpts",
        refs,
        refs.map(
          (r) =>
            `[doc:${r.origin.id}@${r.origin.version}#${r.origin.location}]`,
        ),
      );
    },
  };
}

export function memoryTools(
  provider: MemoryProvider,
  env: MemoryEnv,
): readonly ToolImpl[] {
  return [
    forgetMemory(provider, env),
    saveMemory(provider, env),
    searchMemory(provider, env),
  ];
}

export function knowledgeTools(
  provider: KnowledgeProvider,
  env: MemoryEnv,
): readonly ToolImpl[] {
  return [searchKnowledge(provider, env)];
}
