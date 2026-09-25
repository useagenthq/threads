import { z } from "zod";
import type { BranchId } from "../log";
import { Int } from "../log";
import type { Strict } from "../log/zod-types";
import { ok, type Result } from "../result";
import type { LogError } from "../verify/error";
import { READ_ONLY, type StoreDriver } from "./driver";
import { parseRows } from "./tables";

// Durable observer cursors (store.sql observer_cursors): how far each observer
// got on a branch. Only observer bookkeeping: the log never depends on it.

const CursorRow: Strict<{ seq: typeof Int }> = z.strictObject({ seq: Int });

export class ObserverCursors {
  readonly #db: StoreDriver;

  constructor(db: StoreDriver) {
    this.#db = db;
  }

  /** The last seq `observer` handled on the branch; 0 before its first event. */
  async get(
    observer: string,
    branchId: BranchId,
  ): Promise<Result<number, LogError>> {
    const rows = parseRows(
      CursorRow,
      await this.#db.transaction(
        (tx) =>
          tx.all(
            "SELECT seq FROM observer_cursors WHERE observer = ? AND branch_id = ?",
            [observer, branchId],
          ),
        READ_ONLY,
      ),
    );
    return rows.ok ? ok(rows.value[0]?.seq ?? 0) : rows;
  }

  /** Moves the cursor forward only: a late or repeated write never moves it back. */
  advance(observer: string, branchId: BranchId, seq: number): Promise<void> {
    return this.advanceAll(observer, [{ branch_id: branchId, seq }]);
  }

  /** `advance` for several branches, in one transaction. */
  async advanceAll(
    observer: string,
    rows: readonly { readonly branch_id: BranchId; readonly seq: number }[],
  ): Promise<void> {
    await this.#db.transaction(async (tx) => {
      for (const { branch_id: branchId, seq } of rows)
        await tx.run(
          `INSERT INTO observer_cursors (observer, branch_id, seq) VALUES (?, ?, ?)
         ON CONFLICT (observer, branch_id) DO UPDATE SET seq =
           CASE WHEN observer_cursors.seq > excluded.seq THEN observer_cursors.seq
           ELSE excluded.seq END`,
          [observer, branchId, seq],
        );
    });
  }
}
