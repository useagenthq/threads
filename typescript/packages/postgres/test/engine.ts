import type { ArtifactStore, StoreDriver } from "@threads/core/store-driver";
import { pgArtifacts } from "../src/artifacts";
import { openPg } from "../src/driver";
import { freshSchema } from "./kit";

// The Postgres engine for core's store fixtures (THREADS_TEST_STORE=postgres): a driver on a
// schema of its own, and artifacts in its database. Dropped when the driver closes.

export async function pgDriver(): Promise<{
  readonly db: StoreDriver;
  readonly artifacts: ArtifactStore;
}> {
  const { url, drop } = await freshSchema();
  // Many fixtures never close their store: small pools that let go of idle connections fast
  // keep a whole suite under the server's connection limit.
  const pg = openPg(url, {}, { max: 3, idleMs: 200 });
  const db: StoreDriver = {
    ...pg,
    close: async () => {
      await pg.close();
      await drop();
    },
  };
  return { db, artifacts: pgArtifacts(pg) };
}
