import { z } from "zod";
import { sha256Hex } from "../hash";
import { containsSecret } from "../redact";
import { err, ok, type Result } from "../result";
import type { Failure } from "../sandbox/protocol";
import type { ArtifactStore } from "../store/artifacts";
import { READ_ONLY, type StoreDriver, type Tx } from "../store/driver";
import { installFts, matchQuery } from "./fts";
import { decodeText, passages } from "./passages";
import type {
  DocVersion,
  KnowledgeHit,
  KnowledgeProvider,
  KnowledgeSource,
  ProviderError,
  Scope,
} from "./protocol";
import { rebuildIndex } from "./rebuild";

// localKnowledge() (spec/api.json): admitted versions stored as artifacts,
// an FTS5 index over their passages, and one monotonic revision. Versions are immutable and
// removes are tombstones, so a search can be answered as of any revision (a pinned fork). The
// index is a view: `rebuild` refills it from the admitted artifacts. The tables and version ids
// are the Python store's, so one store serves both languages.

const DDL = `
CREATE TABLE IF NOT EXISTS local_knowledge_docs (
  doc_id TEXT NOT NULL, version TEXT NOT NULL, revision INTEGER NOT NULL,
  removed_revision INTEGER, content_sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
  media_type TEXT NOT NULL, location TEXT, namespace TEXT NOT NULL, record_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL, agent TEXT NOT NULL, scope TEXT NOT NULL,
  UNIQUE (tenant_id, agent, scope, doc_id, version)
) STRICT;
CREATE TABLE IF NOT EXISTS local_knowledge_keys (
  key TEXT PRIMARY KEY, digest TEXT NOT NULL, doc_id TEXT NOT NULL, version TEXT NOT NULL,
  revision INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS local_knowledge_revision (
  one INTEGER PRIMARY KEY CHECK (one = 1), revision INTEGER NOT NULL
) STRICT;
CREATE VIRTUAL TABLE IF NOT EXISTS local_knowledge_fts
  USING fts5(text, doc_rowid UNINDEXED, span_start UNINDEXED, span_end UNINDEXED);
`;
const SCOPE = "d.tenant_id = ? AND d.agent = ? AND d.scope = ?";
/** The latest admitted version of each doc as of a revision, unless removed by then. */
const LIVE = `
d.revision <= ? AND (d.removed_revision IS NULL OR d.removed_revision > ?)
AND d.revision = (SELECT max(e.revision) FROM local_knowledge_docs e
  WHERE e.doc_id = d.doc_id AND e.tenant_id = d.tenant_id AND e.agent = d.agent
  AND e.scope = d.scope AND e.revision <= ?) AND ${SCOPE}`;
const SEARCH = `
SELECT d.doc_id, d.version, f.span_start, f.span_end, f.text, d.namespace, d.record_id,
  bm25(local_knowledge_fts) AS rank
FROM local_knowledge_fts f JOIN local_knowledge_docs d ON d.rowid = f.doc_rowid
WHERE local_knowledge_fts MATCH ? AND ${LIVE}`;
const TEXT = ["text/", "application/json"] as const;

const Revision = z.object({ revision: z.int() });
const KeyRow = z.object({
  digest: z.string(),
  doc_id: z.string(),
  version: z.string(),
  revision: z.int(),
});
const RowId = z.object({ rowid: z.int() });
const HitRow = z.object({
  doc_id: z.string(),
  version: z.string(),
  span_start: z.int(),
  span_end: z.int(),
  text: z.string(),
  namespace: z.string(),
  record_id: z.string(),
  rank: z.number(),
});
const DocRow = z.object({
  content_sha256: z.string(),
  bytes: z.int(),
  media_type: z.string(),
  namespace: z.string(),
  record_id: z.string(),
});

type Local = KnowledgeProvider & {
  /** Drops the index and refills it from the admitted artifacts (`rebuildIndex`). */
  readonly rebuild: () => Promise<Result<void, ProviderError>>;
};

export type LocalKnowledgeOptions = { readonly paths: readonly string[] };

type Binder = (db: StoreDriver, artifacts: ArtifactStore) => Promise<Local>;
const locals = new WeakMap<
  KnowledgeProvider,
  { readonly paths: readonly string[]; readonly bind: Binder }
>();

const unbound = async (): Promise<{
  readonly ok: false;
  readonly error: Failure<"unavailable">;
}> =>
  err({
    code: "unavailable",
    message: "localKnowledge() is bound to a store by a run",
  });

export function localKnowledge(
  options: LocalKnowledgeOptions,
): KnowledgeProvider {
  const provider: KnowledgeProvider = {
    ingest: unbound,
    remove: unbound,
    search: unbound,
    get: unbound,
    revision: unbound,
  };
  locals.set(provider, { paths: options.paths, bind: bound });
  return provider;
}

/** The built-in bound to a run's store with its configured paths, or undefined. */
export async function bindLocalKnowledge(
  provider: KnowledgeProvider,
  db: StoreDriver,
  artifacts: ArtifactStore,
): Promise<
  { readonly local: Local; readonly paths: readonly string[] } | undefined
> {
  const found = locals.get(provider);
  return found === undefined
    ? undefined
    : { local: await found.bind(db, artifacts), paths: found.paths };
}

async function guard<T, E>(
  fn: () => Promise<Result<T, E | Failure<"unavailable">>>,
): Promise<Result<T, E | Failure<"unavailable">>> {
  try {
    return await fn();
  } catch (error) {
    return err({ code: "unavailable", message: String(error) });
  }
}

/** The source's text; anything but UTF-8 text is a visible parse failure (F14.7). */
function parsed(source: KnowledgeSource): Result<string, ProviderError> {
  if (!TEXT.some((t) => source.media_type.startsWith(t)))
    return err({
      code: "invalid",
      message: `can't parse ${source.media_type}`,
    });
  const text = decodeText(source.content);
  return text === undefined
    ? err({ code: "invalid", message: `${source.source_id}: not UTF-8 text` })
    : ok(text);
}

const where = (s: Scope): readonly string[] => [s.tenant_id, s.agent, s.scope];

async function revisionOf(tx: Tx): Promise<number> {
  const rows = Revision.array().parse(
    await tx.all("SELECT revision FROM local_knowledge_revision WHERE one = 1"),
  );
  return rows[0]?.revision ?? 0;
}

async function bump(tx: Tx): Promise<number> {
  const next = (await revisionOf(tx)) + 1;
  await tx.run(
    `INSERT INTO local_knowledge_revision (one, revision) VALUES (1, ?)
     ON CONFLICT (one) DO UPDATE SET revision = excluded.revision`,
    [next],
  );
  return next;
}

async function index(tx: Tx, rowid: number, text: string): Promise<void> {
  for (const p of passages(text))
    await tx.run(
      `INSERT INTO local_knowledge_fts (text, doc_rowid, span_start, span_end)
       VALUES (?, ?, ?, ?)`,
      [p.text, rowid, p.start, p.end],
    );
}

async function admit(
  tx: Tx,
  scope: Scope,
  source: KnowledgeSource,
  key: string,
  text: string,
): Promise<Result<DocVersion, ProviderError>> {
  const digest = sha256Hex(source.content);
  const version = digest.slice(0, 16);
  const [done] = KeyRow.array().parse(
    await tx.all(
      "SELECT digest, doc_id, version, revision FROM local_knowledge_keys WHERE key = ?",
      [key],
    ),
  );
  if (done !== undefined)
    return done.digest === digest && done.doc_id === source.source_id
      ? ok({
          doc_id: done.doc_id,
          version: done.version,
          content_sha256: digest,
          revision: done.revision,
        })
      : err({
          code: "invalid",
          message: `key ${key} reused with other content`,
        });
  const at = await bump(tx);
  const [found] = RowId.array().parse(
    await tx.all(
      `SELECT rowid FROM local_knowledge_docs AS d WHERE doc_id = ? AND version = ? AND ${SCOPE}`,
      [source.source_id, version, ...where(scope)],
    ),
  );
  if (found !== undefined)
    // ponytail: re-admitting removed bytes moves the row's revision, as in Python.
    await tx.run(
      `UPDATE local_knowledge_docs SET revision = ?, removed_revision = NULL
       WHERE rowid = ? AND removed_revision IS NOT NULL`,
      [at, found.rowid],
    );
  else await insertDoc(tx, scope, source, version, at, digest, text);
  await tx.run(
    `INSERT INTO local_knowledge_keys (key, digest, doc_id, version, revision)
     VALUES (?, ?, ?, ?, ?)`,
    [key, digest, source.source_id, version, at],
  );
  return ok({
    doc_id: source.source_id,
    version,
    content_sha256: digest,
    revision: at,
  });
}

async function insertDoc(
  tx: Tx,
  scope: Scope,
  source: KnowledgeSource,
  version: string,
  at: number,
  digest: string,
  text: string,
): Promise<void> {
  const [row] = RowId.array().parse(
    await tx.all(
      `INSERT INTO local_knowledge_docs (doc_id, version, revision, content_sha256, bytes,
       media_type, location, namespace, record_id, tenant_id, agent, scope)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING rowid`,
      [
        source.source_id,
        version,
        at,
        digest,
        source.content.length,
        source.media_type,
        source.location ?? null,
        source.binding.namespace,
        source.binding.record_id,
        ...where(scope),
      ],
    ),
  );
  await index(tx, row?.rowid ?? 0, text);
}

async function bound(
  db: StoreDriver,
  artifacts: ArtifactStore,
): Promise<Local> {
  await installFts(db, DDL, "localKnowledge()", "run this agent on sqlite()");
  return {
    ingest: async (scope, source, key) =>
      guard(async () => {
        const text = parsed(source);
        if (!text.ok) return text;
        // Byte-exact (its digest is its version): a source holding a registered value is refused.
        if (containsSecret(source.content))
          return err({
            code: "invalid",
            message: `${source.source_id}: holds a registered secret; not ingested`,
          });
        // The admitted bytes are durable before the row that references them.
        await artifacts.put(source.content);
        return db.transaction((tx) =>
          admit(tx, scope, source, key, text.value),
        );
      }),
    remove: async (scope, docId, key) =>
      guard(() =>
        db.transaction(async (tx) => {
          const seen = await tx.all(
            "SELECT 1 AS one FROM local_knowledge_keys WHERE key = ?",
            [key],
          );
          if (seen.length > 0) return ok(undefined);
          const at = await bump(tx);
          await tx.run(
            `UPDATE local_knowledge_docs AS d SET removed_revision = ?
             WHERE d.doc_id = ? AND d.removed_revision IS NULL AND ${SCOPE}`,
            [at, docId, ...where(scope)],
          );
          await tx.run(
            `INSERT INTO local_knowledge_keys (key, digest, doc_id, version, revision)
             VALUES (?, 'remove', ?, '', ?)`,
            [key, docId, at],
          );
          return ok(undefined);
        }),
      ),
    search: async (scope, query, options = {}) =>
      guard(() =>
        db.transaction(async (tx) => {
          const match = matchQuery(query);
          if (match === undefined) return ok([]);
          const only = options.sources ?? [];
          const sql = `${SEARCH}${only.length === 0 ? "" : ` AND d.doc_id IN (${only.map(() => "?").join(", ")})`} ORDER BY bm25(local_knowledge_fts) LIMIT ?`;
          const at = options.asOf ?? (await revisionOf(tx));
          const rows = HitRow.array().parse(
            await tx.all(sql, [
              match,
              at,
              at,
              at,
              ...where(scope),
              ...only,
              options.k ?? 5,
            ]),
          );
          return ok(rows.map(hitOf));
        }, READ_ONLY),
      ),
    get: async (scope, docId, version) =>
      guard(async () => {
        const [row] = DocRow.array().parse(
          await db.transaction(
            (tx) =>
              tx.all(
                `SELECT content_sha256, bytes, media_type, namespace, record_id
                 FROM local_knowledge_docs AS d WHERE doc_id = ? AND version = ? AND ${SCOPE}`,
                [docId, version, ...where(scope)],
              ),
            READ_ONLY,
          ),
        );
        if (row === undefined)
          return err({
            code: "not_found" as const,
            message: `no ${docId}@${version} in this scope`,
          });
        return ok({
          doc_id: docId,
          version,
          media_type: row.media_type,
          content_ref: {
            sha256: row.content_sha256,
            bytes: row.bytes,
            media_type: row.media_type,
          },
          binding: { namespace: row.namespace, record_id: row.record_id },
        });
      }),
    revision: async () =>
      guard(async () => ok(await db.transaction(revisionOf, READ_ONLY))),
    rebuild: () => rebuildIndex(db, artifacts, index),
  };
}

function hitOf(r: z.infer<typeof HitRow>): KnowledgeHit {
  return {
    doc_id: r.doc_id,
    version: r.version,
    span: { start: r.span_start, end: r.span_end },
    text: r.text,
    score: -r.rank,
    binding: { namespace: r.namespace, record_id: r.record_id },
  };
}
