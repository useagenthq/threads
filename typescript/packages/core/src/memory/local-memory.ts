import { z } from "zod";
import { sha256Hex } from "../hash";
import { err, ok, type Result } from "../result";
import type { Failure } from "../sandbox/protocol";
import type { SqliteDriver } from "../store/driver";
import { installFts, matchQuery } from "./fts";
import {
  type MemoryHit,
  MemoryOrigin,
  type MemoryProvider,
  type MemoryRecord,
  type ProviderError,
  type Scope,
} from "./protocol";

// localMemory() (spec/api.json): the built-in memory provider, SQLite FTS5 (BM25) in the run's
// own store. Writes are idempotent on their key forever: the same key with the same record is
// a no-op, with another record an error, so a replayed save never duplicates and a replayed
// forget never double-deletes. The tables, ids and digests are the Python store's, byte for
// byte, so one store serves both languages.

const DDL = `
CREATE TABLE IF NOT EXISTS local_memory (
  id TEXT NOT NULL UNIQUE, key TEXT NOT NULL UNIQUE, digest TEXT NOT NULL,
  text TEXT NOT NULL, origin TEXT NOT NULL, provenance TEXT NOT NULL,
  namespace TEXT NOT NULL, record_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL, agent TEXT NOT NULL, scope TEXT NOT NULL,
  forgotten INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE VIRTUAL TABLE IF NOT EXISTS local_memory_fts USING fts5(text);
`;
const RECALL = `
SELECT m.id, m.text, m.origin, m.namespace, m.record_id, bm25(local_memory_fts) AS rank
FROM local_memory_fts JOIN local_memory m ON m.rowid = local_memory_fts.rowid
WHERE local_memory_fts MATCH ? AND m.tenant_id = ? AND m.agent = ? AND m.scope = ?
  AND m.forgotten = 0
ORDER BY bm25(local_memory_fts) LIMIT ?`;

/** The dedup window of a key: the table keeps every key for good. */
const FOREVER_MS = Number.MAX_SAFE_INTEGER;

const HitRow = z.object({
  id: z.string(),
  text: z.string(),
  origin: MemoryOrigin,
  namespace: z.string(),
  record_id: z.string(),
  rank: z.number(),
});
const DigestRow = z.object({ digest: z.string() });
const ForgetRow = z.object({ rowid: z.int(), forgotten: z.int() });
const RowId = z.object({ rowid: z.int() });

const unbound = async (): Promise<{
  readonly ok: false;
  readonly error: Failure<"unavailable">;
}> =>
  err({
    code: "unavailable",
    message: "localMemory() is bound to a store by a run",
  });

const locals = new WeakMap<
  MemoryProvider,
  (db: SqliteDriver) => MemoryProvider
>();

export function localMemory(): MemoryProvider {
  const provider: MemoryProvider = {
    remember: unbound,
    recall: unbound,
    forget: unbound,
    writeEffect: "idempotent",
    dedupWindowMs: FOREVER_MS,
  };
  locals.set(provider, bound);
  return provider;
}

/** A run's memory: the built-in bound to its store (FTS5 checked here), or the provider as is. */
export function bindMemory(
  provider: MemoryProvider,
  db: SqliteDriver,
): MemoryProvider {
  return locals.get(provider)?.(db) ?? provider;
}

function guard<T>(
  fn: () => Result<T, ProviderError>,
): Result<T, ProviderError> {
  try {
    return fn();
  } catch (error) {
    return err({ code: "unavailable", message: String(error) });
  }
}

const where = (s: Scope): readonly string[] => [s.tenant_id, s.agent, s.scope];

/** The record as the Python store hashes it: its fields in schema order, compact JSON. */
function digestOf(r: MemoryRecord): string {
  return sha256Hex(
    JSON.stringify({
      text: r.text,
      origin: r.origin,
      provenance: {
        thread_id: r.provenance.thread_id,
        event_ids: r.provenance.event_ids,
      },
      binding: {
        namespace: r.binding.namespace,
        record_id: r.binding.record_id,
      },
    }),
  );
}

function bound(db: SqliteDriver): MemoryProvider {
  installFts(db, DDL, "localMemory()");
  const insert = (
    scope: Scope,
    record: MemoryRecord,
    key: string,
    id: string,
    digest: string,
  ): void => {
    db.run(
      `INSERT INTO local_memory (id, key, digest, text, origin, provenance, namespace,
       record_id, tenant_id, agent, scope) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      [
        id,
        key,
        digest,
        record.text,
        record.origin,
        JSON.stringify(record.provenance),
        record.binding.namespace,
        record.binding.record_id,
        ...where(scope),
      ],
    );
    const [row] = RowId.array().parse(
      db.all("SELECT rowid FROM local_memory WHERE key = ?", [key]),
    );
    db.run("INSERT INTO local_memory_fts (rowid, text) VALUES (?, ?)", [
      row?.rowid ?? null,
      record.text,
    ]);
  };
  return {
    writeEffect: "idempotent",
    dedupWindowMs: FOREVER_MS,
    remember: async (scope, record, key) =>
      guard(() => {
        const digest = digestOf(record);
        const ref = { id: `mem_${sha256Hex(key).slice(0, 24)}`, version: "1" };
        return db.transaction(() => {
          const [done] = DigestRow.array().parse(
            db.all("SELECT digest FROM local_memory WHERE key = ?", [key]),
          );
          if (done === undefined) insert(scope, record, key, ref.id, digest);
          else if (done.digest !== digest)
            return err({ code: "invalid", message: `key ${key} reused` });
          return ok(ref);
        });
      }),
    recall: async (scope, query, options = {}) =>
      guard(() => {
        const match = matchQuery(query);
        if (match === undefined) return ok([]);
        const rows = HitRow.array().parse(
          db.all(RECALL, [match, ...where(scope), options.k ?? 5]),
        );
        return ok(
          rows.map(
            (r): MemoryHit => ({
              id: r.id,
              version: "1",
              text: r.text,
              score: -r.rank,
              origin: r.origin,
              binding: { namespace: r.namespace, record_id: r.record_id },
            }),
          ),
        );
      }),
    forget: async (scope, id) =>
      guard(() =>
        db.transaction(() => {
          const [row] = ForgetRow.array().parse(
            db.all(
              `SELECT rowid, forgotten FROM local_memory
               WHERE id = ? AND tenant_id = ? AND agent = ? AND scope = ?`,
              [id, ...where(scope)],
            ),
          );
          if (row === undefined)
            return err({
              code: "invalid",
              message: `no memory ${id} in this scope`,
            });
          // Forgetting twice is a no-op.
          if (row.forgotten === 0) {
            db.run("UPDATE local_memory SET forgotten = 1 WHERE rowid = ?", [
              row.rowid,
            ]);
            db.run("DELETE FROM local_memory_fts WHERE rowid = ?", [row.rowid]);
          }
          return ok(undefined);
        }),
      ),
  };
}
