import { z } from "zod";
import { Int } from "../log";
import type { ArtifactStore } from "./artifacts";
import { READ_ONLY, type StoreDriver } from "./driver";
import { parseRows } from "./tables";

export { sweepFiles } from "./artifact-sweep";

// Retention: the artifact half of `threads gc` (`threads delete` is deletion.ts). Nothing is
// deleted automatically.

const SHA = /"sha256":"([0-9a-f]{64})"/g;
const Line = z.strictObject({
  branch_id: z.string(),
  seq: Int,
  line: z.instanceof(Uint8Array),
});
const Dropped = z.strictObject({ id: z.string() });

/** Rows per read: each page is its own short read-only transaction. */
const PAGE = 1000;

/**
 * The artifact sweep of `threads gc`: every artifact some stored line names (events, headers,
 * a torn import's dropped bytes) is kept; any other older than `olderThan` is removed. Returns
 * the removed hashes. An artifact put after the scan is younger than any sane `olderThan`, so
 * the grace window keeps it until its referencing append commits.
 */
export async function sweepArtifacts(
  db: StoreDriver,
  artifacts: ArtifactStore,
  olderThan: number,
): Promise<readonly string[]> {
  return artifacts.sweep(await referenced(db), olderThan);
}

/** Every artifact hash a stored line or a torn import's dropped bytes name. */
async function referenced(db: StoreDriver): Promise<ReadonlySet<string>> {
  const kept = new Set<string>();
  let after: readonly [string, number] | undefined = ["", -1];
  while (after !== undefined) after = await page(db, after, kept);
  const dropped = await db.transaction(
    (tx) =>
      tx.all(
        "SELECT dropped_ref AS id FROM branches WHERE dropped_ref IS NOT NULL",
      ),
    READ_ONLY,
  );
  const ids = parseRows(Dropped, dropped);
  for (const { id } of ids.ok ? ids.value : []) kept.add(id);
  return kept;
}

/** One page of event lines after `after`, their hashes added; the next page's start, if any. */
async function page(
  db: StoreDriver,
  after: readonly [string, number],
  kept: Set<string>,
): Promise<readonly [string, number] | undefined> {
  const rows = parseRows(
    Line,
    await db.transaction(
      (tx) =>
        tx.all(
          `SELECT branch_id, seq, line FROM events WHERE branch_id > ?
            OR (branch_id = ? AND seq > ?) ORDER BY branch_id, seq LIMIT ${PAGE}`,
          [after[0], after[0], after[1]],
        ),
      READ_ONLY,
    ),
  );
  // A row that can't be read would hide the artifacts it names: never sweep past it.
  if (!rows.ok) throw new Error(rows.error.message);
  for (const { line } of rows.value)
    for (const m of utf8.decode(line).matchAll(SHA))
      if (m[1] !== undefined) kept.add(m[1]);
  const last = rows.value.at(-1);
  return last === undefined || rows.value.length < PAGE
    ? undefined
    : [last.branch_id, last.seq];
}

const utf8 = new TextDecoder();
