import { z } from "zod";
import type { BranchId } from "../log";
import { Int } from "../log";
import type { Strict } from "../log/zod-types";
import { ok, type Result } from "../result";
import type { LogError } from "../verify/error";
import type { SqliteDriver } from "./driver";
import { parseRows } from "./tables";

// Durable observer cursors (store.sql observer_cursors): how far each observer
// got on a branch. Only observer bookkeeping: the log never depends on it.

const CursorRow: Strict<{ seq: typeof Int }> = z.strictObject({ seq: Int });

export class ObserverCursors {
  readonly #db: SqliteDriver;

  constructor(db: SqliteDriver) {
    this.#db = db;
  }

  /** The last seq `observer` handled on the branch; 0 before its first event. */
  get(observer: string, branchId: BranchId): Result<number, LogError> {
    const rows = parseRows(
      CursorRow,
      this.#db.all(
        "SELECT seq FROM observer_cursors WHERE observer = ? AND branch_id = ?",
        [observer, branchId],
      ),
    );
    return rows.ok ? ok(rows.value[0]?.seq ?? 0) : rows;
  }

  /** Moves the cursor forward only: a late or repeated write never moves it back. */
  advance(observer: string, branchId: BranchId, seq: number): void {
    this.#db.run(
      `INSERT INTO observer_cursors (observer, branch_id, seq) VALUES (?, ?, ?)
       ON CONFLICT (observer, branch_id) DO UPDATE SET seq = max(seq, excluded.seq)`,
      [observer, branchId, seq],
    );
  }
}
