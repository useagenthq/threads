import { readdirSync, rmSync, statSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import type { SqliteDriver } from "./driver";
import { parseRows } from "./tables";

// Retention: the artifact half of `threads gc` (`threads delete` is deletion.ts). Nothing is
// deleted automatically.

const Id = z.strictObject({ id: z.string() });

function ids(
  db: SqliteDriver,
  sql: string,
  params: readonly string[],
): string[] {
  const rows = parseRows(Id, db.all(sql, params));
  return rows.ok ? rows.value.map((r) => r.id) : [];
}

const SHA = /"sha256":"([0-9a-f]{64})"/g;
const Line = z.strictObject({ line: z.instanceof(Uint8Array) });

/**
 * The artifact sweep of `threads gc`: every artifact some stored line names (events, headers,
 * a torn import's dropped bytes) is kept; any other older than `olderThan` is removed. Returns
 * the removed hashes.
 */
export function sweepArtifacts(
  db: SqliteDriver,
  root: string,
  olderThan: number,
): readonly string[] {
  const kept = referenced(db);
  const removed: string[] = [];
  const base = join(root, "sha256");
  for (const prefix of safeList(base))
    for (const name of safeList(join(base, prefix))) {
      const file = join(base, prefix, name);
      if (kept.has(name) || statSync(file).mtimeMs >= olderThan) continue;
      rmSync(file);
      removed.push(name);
    }
  return removed;
}

/** Every artifact hash a stored line or a torn import's dropped bytes name. */
function referenced(db: SqliteDriver): ReadonlySet<string> {
  const kept = new Set<string>();
  const decoder = new TextDecoder();
  const lines = parseRows(Line, db.all("SELECT line FROM events", []));
  for (const { line } of lines.ok ? lines.value : [])
    for (const m of decoder.decode(line).matchAll(SHA))
      if (m[1] !== undefined) kept.add(m[1]);
  const dropped = ids(
    db,
    "SELECT dropped_ref AS id FROM branches WHERE dropped_ref IS NOT NULL",
    [],
  );
  for (const id of dropped) kept.add(id);
  return kept;
}

function safeList(dir: string): readonly string[] {
  try {
    return readdirSync(dir);
  } catch {
    return [];
  }
}
