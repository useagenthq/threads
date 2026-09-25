import { createHash } from "node:crypto";
import {
  err,
  type LogError,
  logError,
  ok,
  type Result,
} from "@threads/core/store-driver";
import type pg from "pg";
import { z } from "zod";
import { checkout, translated } from "./errors";
import { STORE_SQL, STORE_VERSION } from "./generated/sql";

// Opening a Postgres store: the schema created once across machines, the version checked. Open
// takes a session-level advisory lock keyed by the schema before its transaction begins, so the
// transaction's snapshot sees whatever an earlier opener committed: N processes opening one empty
// database give one schema and N successful opens, and per-test schemas never wait on each
// other. The version rules are SQLite's; the version is threads_meta.store_version.

/** Postgres 16 is the floor. */
const MIN_SERVER = 160000;

/** sha256("threads-store" || schema), its first 8 bytes as a signed big-endian int64. */
export function lockKey(schema: string): bigint {
  const digest = createHash("sha256").update(`threads-store${schema}`).digest();
  return digest.readBigInt64BE(0);
}

const One = z.array(z.object({ v: z.unknown() })).min(1);

async function one(client: pg.PoolClient, sql: string): Promise<unknown> {
  const { rows } = await translated(() => client.query(sql));
  return One.parse(rows.map((r) => ({ v: Object.values(r)[0] })))[0]?.v;
}

/** Creates the schema in an empty database, else checks its version and the server. */
export async function install(pool: pg.Pool): Promise<Result<void, LogError>> {
  const client = await checkout(pool);
  let broken = true;
  try {
    const schema = String(await one(client, "SELECT current_schema()"));
    const key = lockKey(schema).toString();
    await translated(() =>
      client.query("SELECT pg_advisory_lock($1::bigint)", [key]),
    );
    try {
      const done = await created(client);
      broken = false;
      return done;
    } finally {
      await translated(() =>
        client.query("SELECT pg_advisory_unlock($1::bigint)", [key]),
      );
    }
  } finally {
    client.release(broken);
  }
}

async function created(client: pg.PoolClient): Promise<Result<void, LogError>> {
  await translated(() => client.query("BEGIN ISOLATION LEVEL SERIALIZABLE"));
  try {
    const found = await checked(client);
    await translated(() => client.query(found.ok ? "COMMIT" : "ROLLBACK"));
    return found;
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  }
}

async function checked(client: pg.PoolClient): Promise<Result<void, LogError>> {
  const server = Number(await one(client, "SHOW server_version_num"));
  if (server < MIN_SERVER) {
    const version = String(await one(client, "SHOW server_version"));
    return err(
      logError("unsupported_format", `Postgres ${version} is older than 16`),
    );
  }
  const made = await one(
    client,
    "SELECT to_regclass('threads_meta') IS NOT NULL",
  );
  if (made !== true) {
    await translated(() => client.query(STORE_SQL));
    await translated(() =>
      client.query(
        "INSERT INTO threads_meta (key, value) VALUES ('store_version', $1)",
        [String(STORE_VERSION)],
      ),
    );
    return ok(undefined);
  }
  const { rows } = await translated(() =>
    client.query("SELECT value FROM threads_meta WHERE key = 'store_version'"),
  );
  const version = Number(
    z.array(z.object({ value: z.string() })).parse(rows)[0]?.value ?? 0,
  );
  return versionOk(version);
}

function versionOk(found: number): Result<void, LogError> {
  if (found > STORE_VERSION)
    return err(
      logError(
        "unsupported_format",
        `store schema ${found} is newer than ${STORE_VERSION}`,
      ),
    );
  if (found !== STORE_VERSION)
    return err(
      logError(
        "unsupported_format",
        `this store was created by an earlier threads version (schema ${found}); create a new store`,
      ),
    );
  return ok(undefined);
}
