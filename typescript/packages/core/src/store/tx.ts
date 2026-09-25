import { AsyncLocalStorage } from "node:async_hooks";
import type { Dialect, SqlValue, StoreDriver, Tx } from "./driver";

/**
 * A driver whose every transaction is a savepoint of `outer`: store code that opens its own
 * transactions (a LogStore's appends and reads) runs inside a transaction the caller holds.
 */
export function nestedDriver(outer: Tx): StoreDriver {
  return {
    dialect: outer.dialect,
    transaction: (fn) => outer.transaction(fn),
    install: async () => ({ ok: true, value: undefined }),
    close: async () => undefined,
  };
}

// What both drivers share: the `tx` handle over one connection's statements (savepoints for
// nesting, commit hooks), the FIFO that runs one transaction at a time per connection, and the
// guard that turns a transaction opened inside another on the same connection (which would wait
// on itself forever) into a thrown bug.

/** One connection's raw statements, run in whatever transaction it is in. */
export type Statements = {
  readonly run: (sql: string, params: readonly SqlValue[]) => Promise<number>;
  readonly all: (
    sql: string,
    params: readonly SqlValue[],
  ) => Promise<readonly unknown[]>;
};

/** The hooks an attempt collects, run only after its outermost commit. */
export type Commits = (() => void)[];

/**
 * The `tx` handle of one attempt. In a read-only transaction a `run` is a bug: every write goes
 * through `run`, and a read-only transaction is one nothing writes in.
 */
export function openTx(
  statements: Statements,
  dialect: Dialect,
  readOnly: boolean,
  commits: Commits,
): Tx {
  let savepoints = 0;
  const tx: Tx = {
    dialect,
    run: (sql, params = []) => {
      if (readOnly) throw new Error(`a read-only transaction wrote: ${sql}`);
      return statements.run(sql, params);
    },
    all: (sql, params = []) => statements.all(sql, params),
    transaction: async (fn) => {
      savepoints += 1;
      const name = `threads_sp_${savepoints}`;
      await statements.run(`SAVEPOINT ${name}`, []);
      const mark = commits.length;
      try {
        const done = await fn(tx);
        await statements.run(`RELEASE SAVEPOINT ${name}`, []);
        return done;
      } catch (error) {
        commits.length = mark;
        await statements.run(`ROLLBACK TO SAVEPOINT ${name}`, []);
        await statements.run(`RELEASE SAVEPOINT ${name}`, []);
        throw error;
      }
    },
    afterCommit: (fn) => {
      commits.push(fn);
    },
  };
  return tx;
}

/** One transaction at a time on a connection; the next waits for the one before it. */
export class Fifo {
  #tail: Promise<void> = Promise.resolve();

  async turn(): Promise<() => void> {
    const before = this.#tail;
    const mine = Promise.withResolvers<void>();
    this.#tail = mine.promise;
    await before;
    return mine.resolve;
  }
}

const active = new AsyncLocalStorage<ReadonlySet<object>>();

/**
 * Runs `fn` as a transaction of `owner`. Opening another transaction of the same owner inside
 * it is a bug (nesting goes through `tx.transaction`), thrown at once instead of deadlocking.
 */
export function guarded<T>(owner: object, fn: () => Promise<T>): Promise<T> {
  const open = active.getStore();
  if (open?.has(owner))
    throw new Error(
      "a transaction was opened inside another on the same store: nest through tx.transaction",
    );
  return active.run(new Set([...(open ?? []), owner]), fn);
}
