import { createHash } from "node:crypto";
import { join } from "node:path";
import { type Store, sqlite } from "@threads/core";
import { openBunSqlite } from "@threads/core/bun-sqlite";
import type { ArtifactStore, StoreDriver } from "@threads/core/host";
import { fileArtifacts } from "../../../core/src/store";

// The drills' store, by engine (THREADS_TEST_STORE, as the core fixtures): on SQLite, the
// drill directory's shared file; on Postgres, a schema named after the drill directory, one
// connection pool per process and no shared disk, artifacts in the database. That is the
// multi-machine claim: processes share nothing but the database.

export const POSTGRES: boolean =
  process.env["THREADS_TEST_STORE"] === "postgres";

function pgUrl(where: string): {
  readonly url: string;
  readonly schema: string;
  readonly base: string;
} {
  const base = process.env["THREADS_TEST_POSTGRES_URL"];
  if (base === undefined)
    throw new Error(
      "THREADS_TEST_STORE=postgres needs THREADS_TEST_POSTGRES_URL",
    );
  const schema = `j_${createHash("sha256").update(where).digest("hex").slice(0, 24)}`;
  const url = new URL(base);
  url.searchParams.set("options", `-c search_path=${schema}`);
  return { url: url.toString(), schema, base };
}

/** The drill's schema, made once whichever process gets there first. */
async function schemaFor(where: string): Promise<string> {
  const { url, schema, base } = pgUrl(where);
  const { openPg } = await import("../../../postgres/src/driver");
  const admin = openPg(base);
  try {
    await admin.transaction((tx) =>
      tx.run(`CREATE SCHEMA IF NOT EXISTS ${schema}`),
    );
  } catch (error) {
    // Two processes creating it at once: the other one won, and it exists.
    if (!(error instanceof Error && "code" in error && error.code === "23505"))
      throw error;
  } finally {
    await admin.close();
  }
  return url;
}

/** The drill's Store, as a host process opens it. */
export async function drillStore(where: string): Promise<Store> {
  if (!POSTGRES) return sqlite(where);
  const { postgres } = await import("../../../postgres/src");
  return postgres(await schemaFor(where));
}

/** A raw connection to the drill's database, and its artifacts. */
export async function drillDriver(where: string): Promise<{
  readonly db: StoreDriver;
  readonly artifacts: ArtifactStore;
}> {
  if (!POSTGRES)
    return {
      db: openBunSqlite(join(where, "threads.db")),
      artifacts: fileArtifacts(join(where, "artifacts")),
    };
  const { openPg } = await import("../../../postgres/src/driver");
  const { pgArtifacts } = await import("../../../postgres/src/artifacts");
  const db = openPg(await schemaFor(where));
  return { db, artifacts: pgArtifacts(db) };
}
