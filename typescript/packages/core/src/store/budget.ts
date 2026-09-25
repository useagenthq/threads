import { z } from "zod";
import { Int } from "../log";
import type { Strict } from "../log/zod-types";
import { READ_ONLY, type StoreDriver, type Tx } from "./driver";
import { parseRows } from "./tables";

// The tree-wide budget ledger (store.sql budget_ledger): one row per budget,
// limit and model attempt. A reservation sums each covering budget's rows and inserts its own
// rows only if every one fits, in one transaction, so concurrent threads of a tree (one per
// writer, possibly in several processes) can't overspend together.

export type LimitName =
  | "max_cost_nanos"
  | "max_input_tokens"
  | "max_output_tokens"
  | "max_model_requests";

/** One budget's limit and what the attempt reserves against it. */
export type Claim = {
  readonly budgetId: string;
  readonly limit: LimitName;
  readonly max: number;
  readonly amount: number;
};

/** The first claim that didn't fit, and the total it would have made. */
export type Refused = { readonly claim: Claim; readonly observed: number };

const Sum: Strict<{ total: typeof Int }> = z.strictObject({ total: Int });
const Key: Strict<{ attempt_key: z.ZodString }> = z.strictObject({
  attempt_key: z.string(),
});

export class BudgetLedger {
  readonly #db: StoreDriver;

  constructor(db: StoreDriver) {
    this.#db = db;
  }

  /** All of `claims` under `attemptKey`, or none. Reserving the same key again replaces it. */
  reserve(
    attemptKey: string,
    claims: readonly Claim[],
  ): Promise<Refused | undefined> {
    // Keyed by (budget, limit, attempt): a reservation done again replaces itself, so a commit
    // whose outcome is unknown can be retried.
    return this.#db.transaction(async (tx) => {
      for (const claim of claims) {
        const observed = (await spent(tx, claim, attemptKey)) + claim.amount;
        if (observed > claim.max) return { claim, observed };
      }
      for (const c of claims) await put(tx, attemptKey, c);
      return undefined;
    });
  }

  /**
   * The attempt's disposition replaces its reservation in every budget it was reserved against
   *. Idempotent, so a restarted writer can settle again.
   */
  async settle(
    attemptKey: string,
    amounts: readonly (readonly [LimitName, number])[],
  ): Promise<void> {
    await this.#db.transaction(async (tx) => {
      for (const [limit, amount] of amounts)
        await tx.run(
          `UPDATE budget_ledger SET amount = ?, state = 'settled'
           WHERE attempt_key = ? AND limit_name = ?`,
          [amount, attemptKey, limit],
        );
    });
  }

  /** Attempt keys under `prefix` still reserved: their attempts have no disposition yet. */
  async reserved(prefix: string): Promise<readonly string[]> {
    const rows = parseRows(
      Key,
      await this.#db.transaction(
        (tx) =>
          tx.all(
            `SELECT DISTINCT attempt_key FROM budget_ledger
             WHERE state = 'reserved' AND ${PREFIXED}`,
            [prefix, prefix],
          ),
        READ_ONLY,
      ),
    );
    return rows.ok ? rows.value.map((r) => r.attempt_key) : [];
  }

  /** Attempt keys under `prefix` with any row, reserved or settled. */
  async keys(prefix: string): Promise<ReadonlySet<string>> {
    const rows = parseRows(
      Key,
      await this.#db.transaction(
        (tx) =>
          tx.all(
            `SELECT DISTINCT attempt_key FROM budget_ledger WHERE ${PREFIXED}`,
            [prefix, prefix],
          ),
        READ_ONLY,
      ),
    );
    return new Set(rows.ok ? rows.value.map((r) => r.attempt_key) : []);
  }

  /**
   * Re-enters an attempt the log has and the ledger lacks (a lost or imported ledger), unchecked:
   * it was dispatched already, so it counts whether or not it fits.
   */
  async restore(attemptKey: string, claims: readonly Claim[]): Promise<void> {
    await this.#db.transaction(async (tx) => {
      for (const c of claims)
        await tx.run(
          `INSERT INTO budget_ledger (budget_id, limit_name, attempt_key, amount, state)
           VALUES (?, ?, ?, ?, 'reserved') ON CONFLICT DO NOTHING`,
          [c.budgetId, c.limit, attemptKey, c.amount],
        );
    });
  }

  /** What `budgetId` has reserved and settled against `limit`. */
  spent(budgetId: string, limit: LimitName): Promise<number> {
    return this.#db.transaction(
      (tx) => spentIn(tx, budgetId, limit),
      READ_ONLY,
    );
  }

  /** Drops a reservation whose attempt never reached the log. */
  async release(attemptKey: string): Promise<void> {
    await this.#db.transaction((tx) =>
      tx.run("DELETE FROM budget_ledger WHERE attempt_key = ?", [attemptKey]),
    );
  }
}

// A prefix match with no LIKE wildcards; the cast types the parameter for Postgres.
const PREFIXED = "substr(attempt_key, 1, length(CAST(? AS TEXT))) = ?";

async function spent(tx: Tx, claim: Claim, except: string): Promise<number> {
  const rows = parseRows(
    Sum,
    await tx.all(
      `SELECT CAST(COALESCE(SUM(amount), 0) AS BIGINT) AS total FROM budget_ledger
       WHERE budget_id = ? AND limit_name = ? AND attempt_key <> ?`,
      [claim.budgetId, claim.limit, except],
    ),
  );
  if (!rows.ok) throw new Error(rows.error.message);
  return rows.value[0]?.total ?? 0;
}

/** `BudgetLedger.spent` in a transaction the caller holds (a decided append's headroom). */
export function spentIn(
  tx: Tx,
  budgetId: string,
  limit: LimitName,
): Promise<number> {
  return spent(tx, { budgetId, limit, max: 0, amount: 0 }, "");
}

async function put(tx: Tx, key: string, c: Claim): Promise<void> {
  await tx.run(
    `INSERT INTO budget_ledger (budget_id, limit_name, attempt_key, amount, state)
     VALUES (?, ?, ?, ?, 'reserved')
     ON CONFLICT (budget_id, limit_name, attempt_key)
     DO UPDATE SET amount = excluded.amount, state = 'reserved'`,
    [c.budgetId, c.limit, key, c.amount],
  );
}
