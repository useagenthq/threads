import { test } from "bun:test";
import pg from "pg";

// The Postgres leg of the tests: THREADS_TEST_POSTGRES_URL names a server (CI's service
// container); each test gets a schema of its own. Without the URL the leg is skipped with a
// visible reason, unless THREADS_TEST_POSTGRES_REQUIRED=1 (CI), where it fails.

export const PG_URL: string | undefined =
  process.env["THREADS_TEST_POSTGRES_URL"];

if (
  PG_URL === undefined &&
  process.env["THREADS_TEST_POSTGRES_REQUIRED"] === "1"
)
  throw new Error(
    "THREADS_TEST_POSTGRES_REQUIRED=1 but THREADS_TEST_POSTGRES_URL is not set",
  );

if (PG_URL === undefined)
  console.warn(
    "threads: the Postgres tests are skipped: set THREADS_TEST_POSTGRES_URL to run them",
  );

/** `test` when a Postgres server is configured, else a skipped test. */
export const pgTest: typeof test = PG_URL === undefined ? test.skip : test;

/** A fresh schema on the test server, and the URL whose connections use it. */
export async function freshSchema(): Promise<{
  readonly url: string;
  readonly drop: () => Promise<void>;
}> {
  if (PG_URL === undefined) throw new Error("no THREADS_TEST_POSTGRES_URL");
  const schema = `t_${crypto.randomUUID().replaceAll("-", "")}`;
  const admin = new pg.Client({ connectionString: PG_URL });
  await admin.connect();
  await admin.query(`CREATE SCHEMA ${schema}`);
  await admin.end();
  const url = new URL(PG_URL);
  url.searchParams.set("options", `-c search_path=${schema}`);
  return {
    url: url.toString(),
    drop: async () => {
      const again = new pg.Client({ connectionString: PG_URL });
      await again.connect();
      await again.query(`DROP SCHEMA ${schema} CASCADE`);
      await again.end();
    },
  };
}
