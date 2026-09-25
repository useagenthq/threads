import {
  type ArtifactStore,
  READ_ONLY,
  type StoreDriver,
  StoreError,
  sha256Hex,
  verified,
} from "@threads/core/store-driver";
import { z } from "zod";
import type { PgDriver } from "./driver";

// Artifacts as rows of the `artifacts` table: content-addressed, re-hashed on every read, and
// shared by every machine on the database. created_at is the putting process's clock, refreshed
// by a re-put (as a re-link refreshes a file's mtime), and gc's grace window is measured
// against it. A put runs in its own transaction, before the event that names it commits.
// ponytail: a bytea is held whole on put and get (scope cut 3); a streaming blob store is the
// follow-up for multi-gigabyte artifacts.

/** gc reads candidates a page at a time, so no scan holds a long serializable transaction. */
const PAGE = 1000;

const Sha = z.array(z.object({ sha256: z.string() }));
const Bytes = z.array(z.object({ bytes: z.instanceof(Uint8Array) }));

export function pgArtifacts(
  driver: PgDriver,
  now: () => number = Date.now,
): ArtifactStore {
  const db = { transaction: driver.detached };
  const put = async (bytes: Uint8Array): Promise<string> => {
    const sha256 = sha256Hex(bytes);
    const stored = Sha.parse(
      await db.transaction((tx) =>
        tx.all(
          `INSERT INTO artifacts (sha256, size, bytes, created_at) VALUES (?, ?, ?, ?)
            ON CONFLICT (sha256) DO UPDATE SET created_at = EXCLUDED.created_at
            RETURNING encode(sha256(bytes), 'hex') AS sha256`,
          [sha256, bytes.length, bytes, now()],
        ),
      ),
    );
    // A row already there holding other bytes: as a file failing its hash on EEXIST.
    if (stored[0]?.sha256 !== sha256)
      throw new StoreError(`artifact ${sha256} is stored with other bytes`);
    return sha256;
  };
  return {
    put,
    get: async (sha256) => {
      const rows = Bytes.parse(
        await db.transaction(
          (tx) =>
            tx.all("SELECT bytes FROM artifacts WHERE sha256 = ?", [sha256]),
          READ_ONLY,
        ),
      );
      const found = rows[0]?.bytes;
      return verified(
        sha256,
        found === undefined ? undefined : new Uint8Array(found),
      );
    },
    sink: () => {
      const chunks: Uint8Array[] = [];
      return {
        write: (chunk) => {
          chunks.push(chunk.slice());
        },
        finish: async () => {
          const bytes = Buffer.concat(chunks);
          return { sha256: await put(bytes), bytes: bytes.length };
        },
        abort: () => {
          chunks.length = 0;
        },
      };
    },
    sweep: async (keep, olderThan) => {
      const removed: string[] = [];
      let after = "";
      for (;;) {
        const page = await candidates(db, olderThan, after);
        const last = page.at(-1);
        if (last === undefined) return removed;
        after = last;
        removed.push(
          ...(await deleted(
            db,
            page.filter((sha) => !keep.has(sha)),
            olderThan,
          )),
        );
      }
    },
  };
}

type Detached = Pick<StoreDriver, "transaction">;

async function candidates(
  db: Detached,
  olderThan: number,
  after: string,
): Promise<readonly string[]> {
  const rows = Sha.parse(
    await db.transaction(
      (tx) =>
        tx.all(
          `SELECT sha256 FROM artifacts WHERE created_at < ? AND sha256 > ?
            ORDER BY sha256 LIMIT ${PAGE}`,
          [olderThan, after],
        ),
      READ_ONLY,
    ),
  );
  return rows.map((r) => r.sha256);
}

/** Each still older than the window: a re-put since the read refreshed it, and it stays. */
async function deleted(
  db: Detached,
  doomed: readonly string[],
  olderThan: number,
): Promise<readonly string[]> {
  if (doomed.length === 0) return [];
  return db.transaction(async (tx) => {
    const gone: string[] = [];
    for (const sha256 of doomed)
      if (
        (await tx.run(
          "DELETE FROM artifacts WHERE sha256 = ? AND created_at < ?",
          [sha256, olderThan],
        )) === 1
      )
        gone.push(sha256);
    return gone;
  });
}
