import {
  type ArtifactStore,
  memoryArtifacts,
  type StoreDriver,
} from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";

// Which engine the store fixtures run on: THREADS_TEST_STORE=postgres runs the store suite, the
// conformance corpus and the team-op vectors on Postgres (a fresh schema per fixture, on
// THREADS_TEST_POSTGRES_URL), with the same expectations. SQLite is the default.

export type Engine = "sqlite" | "postgres";

export const ENGINE: Engine =
  process.env["THREADS_TEST_STORE"] === "postgres" ? "postgres" : "sqlite";

/** A fresh, empty database of the engine under test, and its artifacts. */
export async function freshDriver(): Promise<{
  readonly db: StoreDriver;
  readonly artifacts: ArtifactStore;
}> {
  if (ENGINE === "sqlite")
    return { db: openBunSqlite(":memory:"), artifacts: memoryArtifacts() };
  // A test-only import across packages: core never depends on the Postgres package.
  const { pgDriver } = await import("../../../postgres/test/engine");
  return pgDriver();
}
