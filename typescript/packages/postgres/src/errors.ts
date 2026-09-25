import { StoreError } from "@threads/core/store-driver";
import type pg from "pg";

// pg's errors as the store's. A serialization failure or deadlock is retried whole; a lost
// connection, exhausted resources, a shutdown or a system error is an outage (StoreError); a
// constraint or syntax error throws as itself.

const RETRYABLE: ReadonlySet<string> = new Set(["40001", "40P01"]);
const OUTAGES = ["08", "53", "57P0", "58"] as const;

/** A broken invariant found while reading a row (a bug): never mistaken for an outage. */
export class Invariant extends Error {
  override readonly name: string = "Invariant";
}

/** A serialization failure or deadlock: nothing committed, the attempt may run again. */
export class Retryable extends Error {
  override readonly name: string = "Retryable";
}

/** The SQLSTATE of a server error; undefined for a client or socket error (no answer at all). */
export function sqlstate(error: unknown): string | undefined {
  if (!(error instanceof Error) || !("code" in error)) return undefined;
  const { code } = error;
  return typeof code === "string" && /^[0-9A-Z]{5}$/.test(code)
    ? code
    : undefined;
}

export function retryable(error: unknown): boolean {
  const state = sqlstate(error);
  return state !== undefined && RETRYABLE.has(state);
}

/** No SQLSTATE (the server never answered) or an outage class. */
export function outage(error: unknown): boolean {
  const state = sqlstate(error);
  return state === undefined || OUTAGES.some((p) => state.startsWith(p));
}

/** Runs a pg call, turning its errors into the store's. */
export async function translated<T>(call: () => Promise<T>): Promise<T> {
  try {
    return await call();
  } catch (error) {
    if (retryable(error)) throw new Retryable(String(error), { cause: error });
    const bug =
      error instanceof TypeError ||
      error instanceof RangeError ||
      error instanceof Invariant;
    if (error instanceof Error && !bug && outage(error))
      throw new StoreError(error.message, { cause: error });
    throw error;
  }
}

/**
 * A pooled client. A connection lost while checked out is reported by the failing query, so
 * its error event is ignored instead of crashing the process.
 */
export async function checkout(pool: pg.Pool): Promise<pg.PoolClient> {
  const client = await translated(() => pool.connect());
  if (client.listenerCount("error") === 0) client.on("error", () => undefined);
  return client;
}
