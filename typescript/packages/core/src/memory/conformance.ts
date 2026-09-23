import { sha256Hex } from "../hash";
import { ThreadId } from "../log";
import { ok, type Result } from "../result";
import { within } from "../sandbox/remote/fence";
import type {
  Binding,
  KnowledgeProvider,
  MemoryProvider,
  MemoryRecord,
  Scope,
} from "./protocol";
import { KnowledgeHit, MemoryHit } from "./protocol";

// The provider conformance suites (F14.8): what every memory and
// knowledge provider must do, the built-ins included. Each takes the host's test function, so
// any runner works (bun:test, vitest, pytest's twin). A case fails by throwing.

export type Runner = {
  readonly test: (name: string, body: () => Promise<void>) => void;
};

const A: Scope = { tenant_id: "tenant-a", agent: "suite", scope: "user:1" };
const B: Scope = { tenant_id: "tenant-b", agent: "suite", scope: "user:1" };
const ERRORS = new Set([
  "unavailable",
  "timeout",
  "invalid",
  "scope_violation",
  "not_found",
]);
const encoder = new TextEncoder();

function check(ok: boolean, what: string): void {
  if (!ok) throw new Error(`provider conformance: ${what}`);
}

/** The value, or a failure naming the typed error; a malformed error fails the case too. */
function value<T>(
  result: Result<T, { readonly code: string; readonly message: string }>,
  what: string,
): T {
  if (result.ok) return result.value;
  check(
    ERRORS.has(result.error.code),
    `${what}: untyped error ${result.error.code}`,
  );
  throw new Error(
    `provider conformance: ${what}: ${result.error.code}: ${result.error.message}`,
  );
}

/** The host calls a provider under a live lease: its transport's fence passes. */
const LIVE = { fence: async () => ok(undefined) };
async function live<T>(call: () => Promise<T>): Promise<T> {
  const done = await within(LIVE, call);
  if (!done.ok) throw done.error.error;
  return done.value;
}

function liveMemory(p: MemoryProvider): MemoryProvider {
  return {
    ...p,
    remember: (...a) => live(() => p.remember(...a)),
    recall: (...a) => live(() => p.recall(...a)),
    forget: (...a) => live(() => p.forget(...a)),
  };
}

function liveKnowledge(p: KnowledgeProvider): KnowledgeProvider {
  return {
    ...p,
    ingest: (...a) => live(() => p.ingest(...a)),
    remove: (...a) => live(() => p.remove(...a)),
    search: (...a) => live(() => p.search(...a)),
    get: (...a) => live(() => p.get(...a)),
    revision: (...a) => live(() => p.revision(...a)),
  };
}

const binding = (n: number): Binding => ({
  namespace: "suite-namespace",
  record_id: `suite-record-${n}`,
});

function record(text: string, n: number): MemoryRecord {
  return {
    text,
    origin: "user",
    provenance: {
      thread_id: ThreadId.parse("0192a000-0000-7000-8000-000000000001"),
      event_ids: [],
    },
    binding: binding(n),
  };
}

/**
 * The memory suite. `make` returns a fresh, empty provider for each case. Providers that
 * declare `writeEffect: "idempotent"` also prove the keyed-write rules.
 */
export function memoryProviderSuite(
  name: string,
  make: () => Promise<MemoryProvider>,
  runner: Runner,
): void {
  const { test } = runner;
  test(`${name}: recall finds a remembered record and echoes its binding`, async () => {
    const p = liveMemory(await make());
    const ref = value(
      await p.remember(A, record("the deploy day is friday", 1), "k1"),
      "remember",
    );
    const hits = MemoryHit.array().parse(
      value(await p.recall(A, "deploy day", { k: 5 }), "recall"),
    );
    const hit = hits.find((h) => h.id === ref.id);
    check(hit !== undefined, "the remembered record is recalled");
    check(hit?.text === "the deploy day is friday", "the text is exact");
    check(hit?.origin === "user", "the origin is kept");
    check(
      hit?.binding.namespace === binding(1).namespace &&
        hit.binding.record_id === binding(1).record_id,
      "the binding is echoed unchanged",
    );
  });
  test(`${name}: another tenant's recall never returns the record`, async () => {
    const p = liveMemory(await make());
    value(
      await p.remember(A, record("tenant a secret launch code", 2), "k2"),
      "remember",
    );
    const hits = MemoryHit.array().parse(
      value(await p.recall(B, "secret launch code"), "recall"),
    );
    check(hits.length === 0, "scope B sees nothing of scope A");
  });
  test(`${name}: a forgotten record is not recalled`, async () => {
    const p = liveMemory(await make());
    const ref = value(
      await p.remember(A, record("the cat is named miso", 3), "k3"),
      "remember",
    );
    value(await p.forget(A, ref.id, "f3"), "forget");
    const hits = MemoryHit.array().parse(
      value(await p.recall(A, "cat named miso"), "recall"),
    );
    check(!hits.some((h) => h.id === ref.id), "forgotten stays forgotten");
  });
  test(`${name}: keyed writes (idempotent providers)`, async () => {
    const p = liveMemory(await make());
    if (p.writeEffect !== "idempotent") return;
    const first = value(
      await p.remember(A, record("tea over coffee", 4), "k4"),
      "remember",
    );
    const again = value(
      await p.remember(A, record("tea over coffee", 4), "k4"),
      "remember again",
    );
    check(first.id === again.id, "the same key and record is a no-op");
    const other = await p.remember(A, record("coffee over tea", 4), "k4");
    check(
      !other.ok && other.error.code === "invalid",
      "the same key with another record is invalid",
    );
    const hits = MemoryHit.array().parse(
      value(await p.recall(A, "tea coffee"), "recall"),
    );
    check(
      hits.filter((h) => h.id === first.id).length === 1,
      "one record, not two",
    );
  });
}

/** The knowledge suite: versioned hits with spans, revisions, tombstones, scope, typed errors. */
export function knowledgeProviderSuite(
  name: string,
  make: () => Promise<KnowledgeProvider>,
  runner: Runner,
): void {
  const { test } = runner;
  const source = (text: string, n: number) => {
    const content = encoder.encode(text);
    return {
      doc: {
        source_id: "guide.md",
        media_type: "text/markdown",
        content,
        binding: binding(n),
      },
      key: `guide.md@${sha256Hex(content)}`,
    };
  };
  test(`${name}: a hit cites an exact version and span of the admitted bytes`, async () => {
    const p = liveKnowledge(await make());
    const s = source("# Refunds\n\nRefunds take five business days.\n", 1);
    const v = value(await p.ingest(A, s.doc, s.key), "ingest");
    check(
      v.content_sha256 === sha256Hex(s.doc.content),
      "the version records the content hash",
    );
    const hits = KnowledgeHit.array().parse(
      value(await p.search(A, "refunds business days"), "search"),
    );
    const hit = hits[0];
    check(
      hit?.doc_id === v.doc_id && hit.version === v.version,
      "the hit names the version",
    );
    const span = new TextDecoder().decode(
      s.doc.content.subarray(hit?.span.start, hit?.span.end),
    );
    check(span === hit?.text, "the span is the excerpt's exact bytes");
    check(
      hit?.binding.record_id === binding(1).record_id,
      "the binding is echoed",
    );
    const doc = value(await p.get(A, v.doc_id, v.version), "get");
    check(
      doc.content_ref.sha256 === v.content_sha256,
      "get returns the admitted artifact",
    );
  });
  test(`${name}: an update is a new version; a search as of the old revision sees the old one`, async () => {
    const p = liveKnowledge(await make());
    const one = source("The office opens at nine.\n", 1);
    const v1 = value(await p.ingest(A, one.doc, one.key), "ingest v1");
    const two = source("The office opens at ten.\n", 2);
    const v2 = value(await p.ingest(A, two.doc, two.key), "ingest v2");
    check(
      v2.revision > v1.revision && v2.version !== v1.version,
      "a new version and revision",
    );
    const now = KnowledgeHit.array().parse(
      value(await p.search(A, "office opens"), "search"),
    );
    check(
      now.every((h) => h.version === v2.version),
      "a live search sees only the new version",
    );
    const then = KnowledgeHit.array().parse(
      value(await p.search(A, "office opens", { asOf: v1.revision }), "as of"),
    );
    check(
      then.some((h) => h.version === v1.version),
      "as of the old revision, the old version",
    );
  });
  test(`${name}: a removed source is excluded from new searches`, async () => {
    const p = liveKnowledge(await make());
    const s = source("Parking is on level two.\n", 1);
    value(await p.ingest(A, s.doc, s.key), "ingest");
    value(await p.remove(A, s.doc.source_id, "remove-1"), "remove");
    const hits = KnowledgeHit.array().parse(
      value(await p.search(A, "parking level"), "search"),
    );
    check(hits.length === 0, "removed means gone from new searches");
  });
  test(`${name}: another tenant's search never returns the document`, async () => {
    const p = liveKnowledge(await make());
    const s = source("The vault code is 4417.\n", 1);
    value(await p.ingest(A, s.doc, s.key), "ingest");
    const hits = KnowledgeHit.array().parse(
      value(await p.search(B, "vault code"), "search"),
    );
    check(hits.length === 0, "scope B sees nothing of scope A");
  });
  test(`${name}: failures are typed values, never throws`, async () => {
    const p = liveKnowledge(await make());
    const missing = await p.get(A, "nope.md", "1");
    check(
      !missing.ok && ERRORS.has(missing.error.code),
      "an unknown version is a typed error",
    );
  });
}
