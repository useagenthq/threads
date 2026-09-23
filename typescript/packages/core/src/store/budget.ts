import { z } from "zod";
import { Int } from "../log";
import type { Strict } from "../log/zod-types";
import type { SqliteDriver } from "./driver";
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
  readonly #db: SqliteDriver;

  constructor(db: SqliteDriver) {
    this.#db = db;
  }

  /** All of `claims` under `attemptKey`, or none. Reserving the same key again replaces it. */
  reserve(attemptKey: string, claims: readonly Claim[]): Refused | undefined {
    return this.#db.transaction(() => {
      for (const claim of claims) {
        const observed = this.#spent(claim, attemptKey) + claim.amount;
        if (observed > claim.max) return { claim, observed };
      }
      for (const c of claims) this.#put(attemptKey, c);
      return undefined;
    });
  }

  /**
   * The attempt's disposition replaces its reservation in every budget it was reserved against
   *. Idempotent, so a restarted writer can settle again.
   */
  settle(
    attemptKey: string,
    amounts: readonly (readonly [LimitName, number])[],
  ): void {
    this.#db.transaction(() => {
      for (const [limit, amount] of amounts)
        this.#db.run(
          `UPDATE budget_ledger SET amount = ?, state = 'settled'
           WHERE attempt_key = ? AND limit_name = ?`,
          [amount, attemptKey, limit],
        );
    });
  }

  /** Attempt keys under `prefix` still reserved: their attempts have no disposition yet. */
  reserved(prefix: string): readonly string[] {
    const rows = parseRows(
      Key,
      this.#db.all(
        `SELECT DISTINCT attempt_key FROM budget_ledger
         WHERE state = 'reserved' AND substr(attempt_key, 1, length(?)) = ?`,
        [prefix, prefix],
      ),
    );
    return rows.ok ? rows.value.map((r) => r.attempt_key) : [];
  }

  /** Attempt keys under `prefix` with any row, reserved or settled. */
  keys(prefix: string): ReadonlySet<string> {
    const rows = parseRows(
      Key,
      this.#db.all(
        `SELECT DISTINCT attempt_key FROM budget_ledger
         WHERE substr(attempt_key, 1, length(?)) = ?`,
        [prefix, prefix],
      ),
    );
    return new Set(rows.ok ? rows.value.map((r) => r.attempt_key) : []);
  }

  /**
   * Re-enters an attempt the log has and the ledger lacks (a lost or imported ledger), unchecked:
   * it was dispatched already, so it counts whether or not it fits.
   */
  restore(attemptKey: string, claims: readonly Claim[]): void {
    this.#db.transaction(() => {
      for (const c of claims)
        this.#db.run(
          `INSERT INTO budget_ledger (budget_id, limit_name, attempt_key, amount, state)
           VALUES (?, ?, ?, ?, 'reserved') ON CONFLICT DO NOTHING`,
          [c.budgetId, c.limit, attemptKey, c.amount],
        );
    });
  }

  /** What `budgetId` has reserved and settled against `limit`. */
  spent(budgetId: string, limit: LimitName): number {
    return this.#spent({ budgetId, limit, max: 0, amount: 0 }, "");
  }

  /** Drops a reservation whose attempt never reached the log. */
  release(attemptKey: string): void {
    this.#db.run("DELETE FROM budget_ledger WHERE attempt_key = ?", [
      attemptKey,
    ]);
  }

  #spent(claim: Claim, except: string): number {
    const rows = parseRows(
      Sum,
      this.#db.all(
        `SELECT COALESCE(SUM(amount), 0) AS total FROM budget_ledger
         WHERE budget_id = ? AND limit_name = ? AND attempt_key != ?`,
        [claim.budgetId, claim.limit, except],
      ),
    );
    if (!rows.ok) throw new Error(rows.error.message);
    return rows.value[0]?.total ?? 0;
  }

  #put(key: string, c: Claim): void {
    this.#db.run(
      `INSERT INTO budget_ledger (budget_id, limit_name, attempt_key, amount, state)
       VALUES (?, ?, ?, ?, 'reserved')
       ON CONFLICT (budget_id, limit_name, attempt_key)
       DO UPDATE SET amount = excluded.amount, state = 'reserved'`,
      [c.budgetId, c.limit, key, c.amount],
    );
  }
}
