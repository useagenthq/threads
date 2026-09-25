import { z } from "zod";
import { containsSecret } from "../redact";
import { err, ok, type Result } from "../result";
import type { ArtifactStore } from "../store/artifacts";
import { READ_ONLY, type StoreDriver, type Tx } from "../store/driver";
import { decodeText } from "./passages";
import type { ProviderError } from "./protocol";

const IndexRow = z.object({ rowid: z.int(), content_sha256: z.string() });

// Sources are read outside the transaction; another connection's ingest in between means
// reading again, a few times, before giving up with unavailable.
const ATTEMPTS = 3;

type Source = {
  readonly rowid: number;
  readonly bytes: Uint8Array;
  readonly text: string;
};

const ADMITTED =
  "SELECT rowid, content_sha256 FROM local_knowledge_docs ORDER BY rowid";

async function admitted(tx: Tx): Promise<string> {
  return JSON.stringify(await tx.all(ADMITTED));
}

async function read(
  db: StoreDriver,
  artifacts: ArtifactStore,
): Promise<readonly Source[]> {
  const rows = IndexRow.array().parse(
    await db.transaction((tx) => tx.all(ADMITTED), READ_ONLY),
  );
  const sources: Source[] = [];
  for (const r of rows) {
    const got = await artifacts.get(r.content_sha256);
    const text = got.ok ? decodeText(got.value) : undefined;
    if (!got.ok || text === undefined)
      throw new Error(
        `an admitted version's artifact is gone: ${r.content_sha256}`,
      );
    sources.push({ rowid: r.rowid, bytes: got.value, text });
  }
  return sources;
}

/**
 * Drops the knowledge index and refills it from the admitted artifacts (F14.3). A source
 * holding a registered value refuses it (invalid) before anything is cleared (C5). The refill
 * runs only if the admitted rows are still the ones it read, so a source another connection
 * admits meanwhile is never dropped from search.
 */
export async function rebuildIndex(
  db: StoreDriver,
  artifacts: ArtifactStore,
  index: (tx: Tx, rowid: number, text: string) => Promise<void>,
): Promise<Result<void, ProviderError>> {
  for (let attempt = 0; attempt < ATTEMPTS; attempt += 1) {
    const seen = await db.transaction(admitted, READ_ONLY);
    const sources = await read(db, artifacts);
    if (sources.some((s) => containsSecret(s.bytes)))
      return err({
        code: "invalid",
        message: "a source holds a registered secret; index kept",
      });
    const done = await db.transaction(async (tx) => {
      if ((await admitted(tx)) !== seen) return false;
      await tx.run("DELETE FROM local_knowledge_fts");
      for (const s of sources) await index(tx, s.rowid, s.text);
      return true;
    });
    if (done) return ok(undefined);
  }
  return err({
    code: "unavailable",
    message: "sources kept changing during the rebuild",
  });
}
