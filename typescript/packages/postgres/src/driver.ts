import {
  type Commits,
  CommitUnknown,
  guarded,
  openTx,
  type Statements,
  type StoreDriver,
  StoreError,
  type TransactionOptions,
  type Tx,
} from "@threads/core/store-driver";
import pg from "pg";
import { postgresUrl, scrubbed } from "./dsn";
import {
  checkout,
  Invariant,
  outage,
  Retryable,
  retryable,
  translated,
} from "./errors";
import { install } from "./install";
import { numbered } from "./placeholders";

// The store on node-postgres: a pool, and every statement inside an explicit SERIALIZABLE
// transaction (READ ONLY for reads). A serialization failure or deadlock re-runs the whole
// transaction function with a doubling jittered pause for up to 5 s, as patient as SQLite's busy
// timeout; after that it is SQLite's busy outcome, a StoreError. An error from COMMIT itself
// leaves the outcome unknown: CommitUnknown, never "nothing written".

/** How long a serialization failure is retried: SQLite's busy timeout. */
export const RETRY_BUDGET_MS = 5_000;
const FIRST_PAUSE_MS = 10;
const MAX_PAUSE_MS = 250;
/** How long a transaction waits for a pooled connection before it is an outage. */
const CONNECT_TIMEOUT_MS = 10_000;

/** Test seams: fault injection at commit, and the retry counter. */
export type Faults = {
  /**
   * Before COMMIT of `attempt`: `retry` answers as a serialization failure (nothing committed),
   * `lost` drops the connection before COMMIT is sent, `after` sends COMMIT and then drops it.
   */
  readonly atCommit?: (
    attempt: number,
  ) => "retry" | "lost" | "after" | undefined;
  /** The retry budget, shortened so a drill that always conflicts ends quickly. */
  readonly retryBudgetMs?: number;
};

export type PgDriver = StoreDriver & {
  /** Serialization retries so far (tests assert on it). */
  readonly retries: () => number;
  /**
   * A transaction on a connection of its own pool, allowed inside another: the artifacts' (their
   * rows are immutable content, so reading one beside an open transaction changes nothing).
   */
  readonly detached: StoreDriver["transaction"];
  readonly pool: pg.Pool;
};

/** int8 as a number; a value past 2^53 - 1 is a broken invariant (a bug), never an outage. */
export function int8(text: string): number {
  const value = Number(text);
  if (!Number.isSafeInteger(value))
    throw new Invariant(`int8 ${text} is outside the safe integer range`);
  return value;
}

const TYPES = {
  getTypeParser: (oid: number, format?: "text" | "binary"): unknown =>
    oid === pg.types.builtins.INT8
      ? int8
      : pg.types.getTypeParser(oid, format ?? "text"),
};

/** The pool's size and how long an idle connection is kept (pg's defaults: 10 and 10 s). */
export type PoolLimits = { readonly max?: number; readonly idleMs?: number };

export function openPg(
  url: string,
  faults: Faults = {},
  limits: PoolLimits = {},
): PgDriver {
  // Two pools: the store's transactions, and the artifacts' detached ones. A detached
  // transaction never nests, so the transactions waiting on it can't starve it of connections.
  const pool = newPool(url, limits);
  const side = newPool(url, limits);
  let retries = 0;
  const budget = faults.retryBudgetMs ?? RETRY_BUDGET_MS;
  const retried = async <T>(
    on: pg.Pool,
    fn: (tx: Tx) => Promise<T>,
    options?: TransactionOptions,
  ): Promise<T> => {
    const began = performance.now();
    for (let attempt = 1; ; attempt += 1) {
      try {
        return await once(on, fn, options, faults, attempt);
      } catch (error) {
        if (!(error instanceof Retryable)) throw error;
        if (performance.now() - began >= budget)
          throw new StoreError(
            `still conflicting after ${attempt} attempts over ${budget} ms`,
            { cause: error },
          );
        retries += 1;
        await sleep(pauseMs(attempt));
      }
    }
  };
  // Every error leaving the driver is scrubbed of the URL and its password.
  const scrubbing =
    <A extends unknown[], T>(fn: (...args: A) => Promise<T>) =>
    async (...args: A): Promise<T> => {
      try {
        return await fn(...args);
      } catch (error) {
        throw scrubbed(error, url);
      }
    };
  const transaction: StoreDriver["transaction"] = scrubbing((fn, options) =>
    retried(pool, fn, options),
  );
  const driver: PgDriver = {
    dialect: "postgres",
    pool,
    retries: () => retries,
    detached: scrubbing((fn, options) => retried(side, fn, options)),
    transaction: (fn, options) =>
      guarded(driver, () => transaction(fn, options)),
    install: scrubbing(() => install(pool)),
    close: async () => {
      await Promise.all([pool.end(), side.end()]);
    },
  };
  return driver;
}

function newPool(url: string, limits: PoolLimits): pg.Pool {
  const pool = new pg.Pool({
    connectionString: url,
    // A second guard: a statement can't run outside SERIALIZABLE even if it escaped a BEGIN.
    // The URL's own options (a search_path, say) are kept.
    options: [
      postgresUrl(url).searchParams.get("options") ?? "",
      "-c default_transaction_isolation=serializable",
    ]
      .join(" ")
      .trim(),
    types: TYPES,
    // A pool that can't hand out a connection is an outage (StoreError), never a hang.
    connectionTimeoutMillis: CONNECT_TIMEOUT_MS,
    ...(limits.max === undefined ? {} : { max: limits.max }),
    ...(limits.idleMs === undefined
      ? {}
      : { idleTimeoutMillis: limits.idleMs }),
  });
  // An idle client's socket error is the next checkout's outage, never a crash.
  pool.on("error", () => undefined);
  return pool;
}

/**
 * The jittered pause before attempt `attempt + 1`: up to 10 ms, doubling to at most 250 ms, so a
 * contended branch is retried for the whole budget without hammering the server.
 */
export function pauseMs(
  attempt: number,
  random: () => number = Math.random,
): number {
  return random() * Math.min(FIRST_PAUSE_MS * 2 ** (attempt - 1), MAX_PAUSE_MS);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function statements(client: pg.PoolClient): Statements {
  return {
    run: async (sql, params) =>
      (await translated(() => client.query(numbered(sql), [...params])))
        .rowCount ?? 0,
    all: async (sql, params) =>
      (await translated(() => client.query(numbered(sql), [...params]))).rows,
  };
}

async function once<T>(
  pool: pg.Pool,
  fn: (tx: Tx) => Promise<T>,
  options: TransactionOptions | undefined,
  faults: Faults,
  attempt: number,
): Promise<T> {
  const client = await checkout(pool);
  let broken = false;
  try {
    const readOnly = options?.readOnly === true;
    await translated(() =>
      client.query(
        `BEGIN ISOLATION LEVEL SERIALIZABLE${readOnly ? " READ ONLY" : ""}`,
      ),
    );
    const commits: Commits = [];
    let done: T;
    try {
      done = await fn(
        openTx(statements(client), "postgres", readOnly, commits),
      );
    } catch (error) {
      broken = !(await rolledBack(client));
      throw error;
    }
    // Until COMMIT answers, the connection may hold an open transaction: never pooled.
    broken = true;
    broken = await commit(client, faults.atCommit?.(attempt));
    for (const hook of commits) hook();
    return done;
  } finally {
    client.release(broken);
  }
}

/** False when the connection is gone: it must not go back to the pool. */
async function rolledBack(client: pg.PoolClient): Promise<boolean> {
  try {
    await client.query("ROLLBACK");
    return true;
  } catch {
    return false;
  }
}

/**
 * COMMIT; true when the connection is broken afterwards. A serialization failure at commit is
 * retried like any other; any other error from COMMIT itself leaves the outcome unknown.
 */
async function commit(
  client: pg.PoolClient,
  fault: "retry" | "lost" | "after" | undefined,
): Promise<boolean> {
  if (fault === "retry") {
    await client.query("ROLLBACK");
    throw new Retryable("forced serialization failure (test)");
  }
  try {
    if (fault === "lost") throw dropped();
    await client.query("COMMIT");
    if (fault === "after") throw dropped();
    return false;
  } catch (error) {
    if (retryable(error)) throw new Retryable(String(error), { cause: error });
    if (error instanceof Error && outage(error))
      throw new CommitUnknown(`COMMIT's outcome is unknown: ${error.message}`, {
        cause: error,
      });
    throw error;
  }
}

function dropped(): Error {
  return new Error("connection dropped (test)");
}
