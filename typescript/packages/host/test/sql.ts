import {
  READ_ONLY,
  type SqlValue,
  type StoreDriver,
  type Tx,
} from "@threads/core/host";

// Raw SQL for tests: each statement in a transaction of its own (the store has no autocommit).

export function sqlAll(
  db: StoreDriver,
  sql: string,
  params: readonly SqlValue[] = [],
): Promise<readonly unknown[]> {
  return db.transaction((tx) => tx.all(sql, params), READ_ONLY);
}

/**
 * `base` with a hook before each write statement, savepoints included: a drill stops a process
 * at a chosen statement of a transaction.
 */
export function beforeRun(
  base: StoreDriver,
  hook: (sql: string, params: readonly SqlValue[]) => void,
): StoreDriver {
  const wrap = (tx: Tx): Tx => ({
    ...tx,
    run: (sql, params = []) => {
      hook(sql, params);
      return tx.run(sql, params);
    },
    transaction: (fn) => tx.transaction((inner) => fn(wrap(inner))),
  });
  return {
    ...base,
    transaction: (fn, options) =>
      base.transaction((tx) => fn(wrap(tx)), options),
  };
}

export function sqlRun(
  db: StoreDriver,
  sql: string,
  params: readonly SqlValue[] = [],
): Promise<number> {
  return db.transaction((tx) => tx.run(sql, params));
}
