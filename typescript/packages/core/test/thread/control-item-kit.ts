import { z } from "zod";
import type { Principal } from "../../src/log";
import type { SqlValue, StoreDriver, Tx } from "../../src/store";
import { reading } from "../../src/store/driver";
import { Crash } from "../team/crash-kit";

// The cross-process cancel drills (lane 29F) share these: a second process over the same
// database, the inbox rows to assert on, and a kill at the commit point right after the item's
// insert.

export const OPERATOR: Principal = {
  issuer: "api",
  tenant: "local",
  subject: "operator",
};

export type Row = {
  readonly inbox_id: number;
  readonly channel: string;
  readonly item: Uint8Array;
  readonly consumed_seq: number | null;
};

const Row: z.ZodType<Row> = z.strictObject({
  inbox_id: z.number(),
  channel: z.string(),
  item: z.instanceof(Uint8Array),
  consumed_seq: z.number().nullable(),
});

/** Every inbox row of the store, oldest first. */
export async function inboxRows(db: StoreDriver): Promise<readonly Row[]> {
  return z
    .array(Row)
    .parse(
      await reading(db, (tx: Tx) =>
        tx.all(
          "SELECT inbox_id, channel, item, consumed_seq FROM inbox ORDER BY inbox_id",
          [],
        ),
      ),
    );
}

/** The dead process's leases are gone: what a restart finds. */
export async function expireLeases(db: StoreDriver): Promise<void> {
  await db.transaction((tx) => tx.run("UPDATE leases SET expires_at = 0", []));
}

/**
 * The same database through a driver that dies once on the first statement after an `inbox`
 * insert: that row is committed, and its requester never runs again.
 */
export function crashAfterInsert(base: StoreDriver): StoreDriver {
  let armed = false;
  let crashed = false;
  const wrap = (tx: Tx): Tx => ({
    ...tx,
    run: async (sql: string, params: readonly SqlValue[] = []) => {
      if (armed && !crashed) {
        crashed = true;
        throw new Crash("killed after the inbox item's insert");
      }
      const done = await tx.run(sql, params);
      armed ||= sql.includes("INSERT INTO inbox");
      return done;
    },
    all: async (sql: string, params: readonly SqlValue[] = []) => {
      if (armed && !crashed) {
        crashed = true;
        throw new Crash("killed after the inbox item's insert");
      }
      return tx.all(sql, params);
    },
    transaction: (fn) => tx.transaction((inner) => fn(wrap(inner))),
  });
  return {
    ...base,
    transaction: (fn, options) =>
      base.transaction((tx) => fn(wrap(tx)), options),
  };
}
