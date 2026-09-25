// @threads/core/internal/feed: the store reader a telemetry exporter builds on. Not public: not in
// spec/api.json, not documented, and it may change in any release. It never takes a lease and
// never appends: its only writes are observer bookkeeping.

import { z } from "zod";
import { openStore, type Store, storeConnection } from "../agent/sqlite";
import { BranchId, Int, ThreadId } from "../log";
import type { Strict } from "../log/zod-types";
import { err, type Result } from "../result";
import { type VerifiedLog, verifyExport } from "../verify";
import { type LogError, logError } from "../verify/error";
import type { ArtifactStore } from "./artifacts";
import { ObserverCursors } from "./cursors";
import { READ_ONLY, type StoreDriver } from "./driver";
import { exportBytes } from "./lines";
import {
  type LossRow,
  markReported,
  registerObserver,
  unreportedLosses,
} from "./losses";
import { parseRows } from "./tables";

export type { EventOf } from "../fold/state";
export { sha256Hex } from "../hash";
export { canonicalize, type Json, type KnownEvent } from "../log";
export { turnOpeners } from "../team";
export { type TelemetryBinding, telemetryBinding } from "../telemetry";
export type { ChainEvent, LogError, VerifiedLog } from "../verify";
export type { LossRow } from "./losses";

const Changed: Strict<{
  branch_id: typeof BranchId;
  thread_id: typeof ThreadId;
  tenant_id: z.ZodString;
  head_seq: typeof Int;
  cursor: typeof Int;
}> = z.strictObject({
  branch_id: BranchId,
  thread_id: ThreadId,
  tenant_id: z.string(),
  head_seq: Int,
  cursor: Int,
});
/** Read failures that say what they are; every other one is reported as log_corrupt. */
const KEPT: ReadonlySet<string> = new Set([
  "unsupported_format",
  "unsupported_critical_event",
  "branch_not_found",
]);

/** A branch with events past the observer's cursor. */
export type ChangedBranch = z.infer<typeof Changed>;

export class Feed {
  readonly #db: StoreDriver;
  readonly #artifacts: ArtifactStore;
  readonly #cursors: ObserverCursors;
  readonly now: () => number;
  readonly observer: string;

  private constructor(
    db: StoreDriver,
    artifacts: ArtifactStore,
    now: () => number,
    observer: string,
  ) {
    this.#db = db;
    this.#artifacts = artifacts;
    this.#cursors = new ObserverCursors(db);
    this.now = now;
    this.observer = observer;
  }

  /** The store's feed for one observer: every tenant's branches. */
  static async open(store: Store, observer: string): Promise<Feed> {
    const { artifacts } = await openStore(store);
    const { db, now } = await storeConnection(store);
    return new Feed(db, artifacts, now, observer);
  }

  /**
   * Every listed branch whose head is past the observer's cursor, in branch id order. A branch
   * without a cursor row starts at its fork point, or 0: a new fork never re-reads its parent.
   */
  async changed(): Promise<Result<readonly ChangedBranch[], LogError>> {
    return parseRows(
      Changed,
      await this.#db.transaction(
        (tx) =>
          tx.all(
            `SELECT b.branch_id, b.thread_id, b.tenant_id, b.head_seq,
            coalesce(c.seq, b.fork_at_seq, 0) AS cursor
          FROM branches b
          LEFT JOIN observer_cursors c ON c.branch_id = b.branch_id AND c.observer = ?
          WHERE b.state NOT IN ('forking', 'fork_failed')
            AND b.head_seq > coalesce(c.seq, b.fork_at_seq, 0)
          ORDER BY b.branch_id`,
            [this.observer],
          ),
        READ_ONLY,
      ),
    );
  }

  /**
   * A branch's verified resolved chain, whatever its tenant. A chain a newer writer made keeps
   * its code (unsupported_format, unsupported_critical_event); any other failure is log_corrupt.
   */
  async chain(branchId: string): Promise<Result<VerifiedLog, LogError>> {
    const bytes = await this.#db.transaction(
      (tx) => exportBytes(tx, this.#artifacts, branchId),
      READ_ONLY,
    );
    const log = bytes.ok ? verifyExport(bytes.value) : bytes;
    if (log.ok || KEPT.has(log.error.code)) return log;
    return err(logError("log_corrupt", log.error.message, log.error.seq));
  }

  /** Moves each cursor forward, never back, in one transaction. */
  async checkpoint(
    rows: readonly { readonly branch_id: BranchId; readonly seq: number }[],
  ): Promise<void> {
    await this.#cursors.advanceAll(this.observer, rows);
  }

  register(): Promise<void> {
    return registerObserver(this.#db, this.observer, this.now());
  }

  unreportedLosses(): Promise<Result<readonly LossRow[], LogError>> {
    return unreportedLosses(this.#db, this.observer);
  }

  markReported(rows: readonly LossRow[]): Promise<void> {
    return markReported(this.#db, this.observer, rows, this.now());
  }
}
