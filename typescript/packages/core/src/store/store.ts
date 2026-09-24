import type { BranchId, SandboxId, ThreadId } from "../log";
import { err, ok, type Result } from "../result";
import { indexImported } from "../team/imported";
import { type VerifiedLog, verifyExport } from "../verify";
import { type LogError, logError } from "../verify/error";
import type { ArtifactStore } from "./artifacts";
import { HostBindings } from "./bindings";
import { newBranch } from "./branch";
import { BudgetLedger } from "./budget";
import { ObserverCursors } from "./cursors";
import type { SqliteDriver } from "./driver";
import * as forking from "./fork-reads";
import {
  beginFork,
  type ForkRequest,
  finishFork,
  reclaimFork,
} from "./fork-writes";
import { importSegments, verifiedImport } from "./import";
import { LEASE_TTL_MS, type StoreAccess, takeLease } from "./lease";
import { ResourceLedger } from "./ledger";
import { exportBytes } from "./lines";
import { ALREADY_OPEN, type BranchOpening, openBranch } from "./open";
import { isTorn, markRepaired, recordRepair } from "./repair";
import {
  atomically,
  type BranchRow,
  installSchema,
  LOCAL_TENANT,
  ownedBranch,
  rootBranch,
  setBranchState,
} from "./tables";
import { Writer, writerMismatch } from "./writer";

export type { ForkRequest } from "./fork-writes";
export { LEASE_TTL_MS } from "./lease";

/**
 * The append-only log store on SQLite: exact line bytes, a head checkpoint per
 * branch updated with every append, one fenced writer per branch, and forks that reference
 * their parent's rows. Every read goes back through the import checks.
 *
 * A store is bound to one tenant: a branch of any other tenant is `branch_not_found`.
 */
export class LogStore {
  readonly #db: SqliteDriver;
  readonly #now: () => number;
  readonly #artifacts: ArtifactStore;
  readonly tenant: string;

  private constructor(
    db: SqliteDriver,
    now: () => number,
    artifacts: ArtifactStore,
    tenantId: string,
  ) {
    this.#db = db;
    this.#now = now;
    this.#artifacts = artifacts;
    this.tenant = tenantId;
  }

  /**
   * Opens the store on `db` for one tenant, creating the store.sql tables. A database a newer
   * schema wrote is `unsupported_format`. `now` is the injected clock for leases, event times
   * and snapshot expiry; `artifacts` keeps the bytes a torn import dropped.
   */
  static open(
    db: SqliteDriver,
    now: () => number,
    artifacts: ArtifactStore,
    tenantId: string = LOCAL_TENANT,
  ): Result<LogStore, LogError> {
    const installed = installSchema(db);
    return installed.ok
      ? ok(new LogStore(db, now, artifacts, tenantId))
      : installed;
  }

  /** Writes a new root branch: its header line, head at seq 0. */
  createBranch(threadId: ThreadId, branchId: BranchId): Result<void, LogError> {
    return atomically(this.#db, () => {
      const made = newBranch(this.#db, {
        tenantId: this.tenant,
        threadId,
        branchId,
        parent: null,
        state: "ready",
        createdAt: this.#now(),
      });
      return made.ok ? ok(undefined) : made;
    });
  }

  /**
   * `branch.open` in a transaction of its own: a new root branch of this tenant with its first
   * events, held by the returned writer at epoch 1. `already_open` when the branch exists.
   */
  openBranch(
    opening: Omit<BranchOpening, "tenantId">,
  ): Result<Writer | typeof ALREADY_OPEN, LogError> {
    const opened = openBranch(this.#db, this.#now(), {
      ...opening,
      tenantId: this.tenant,
    });
    if (!opened.ok) return opened;
    if (opened.value === ALREADY_OPEN) return ok(ALREADY_OPEN);
    const { branchId, lease } = opening;
    return ok(
      new Writer(
        this.#db,
        this.#now,
        { branchId, holderId: lease.holderId, epoch: 1, ttlMs: lease.ttlMs },
        opened.value,
      ),
    );
  }

  /** A branch's state, if this tenant owns it (a forking or failed branch is never listed). */
  branchState(branchId: BranchId): Result<BranchRow["state"], LogError> {
    const row = ownedBranch(this.#db, branchId, this.tenant);
    return row.ok ? ok(row.value.state) : row;
  }

  /** The injected clock this store reads for leases, event times and snapshot expiry. */
  now(): number {
    return this.#now();
  }

  /**
   * The branch's verified resolved chain. Storage is a trust boundary, so this re-verifies. A
   * branch imported without a verified head reads back unverified, with its dropped bytes.
   */
  read(branchId: BranchId): Result<VerifiedLog, LogError> {
    const bytes = this.exportBranch(branchId);
    return bytes.ok ? verifyExport(bytes.value) : bytes;
  }

  /** A thread's main branch, or `branch_not_found`. */
  mainBranch(threadId: ThreadId): Result<BranchId, LogError> {
    const root = rootBranch(this.#db, threadId, this.tenant);
    if (!root.ok) return root;
    return root.value === undefined
      ? err(logError("branch_not_found", `no thread ${threadId}`))
      : ok(root.value);
  }

  /** `threads export`: ancestor segments, the branch's lines, then its head line. */
  exportBranch(branchId: BranchId): Result<Uint8Array, LogError> {
    const owned = ownedBranch(this.#db, branchId, this.tenant);
    if (!owned.ok) return owned;
    return exportBytes(this.#db, this.#artifacts, branchId);
  }

  /**
   * `threads import`: verifies the export, then stores the same bytes, segment by segment. A
   * torn tail's bytes are kept as an artifact, durable before the rows that name them. Every
   * model request must replay from the log and the artifacts already stored (C7, Render v1).
   * The index rows the log holds (wake rows, its teams) are folded again in the same
   * transaction.
   */
  importLog(bytes: Uint8Array): Result<VerifiedLog, LogError> {
    const log = verifiedImport(bytes, this.#artifacts);
    if (!log.ok) return log;
    const torn = log.value.torn;
    const target = {
      tenantId: this.tenant,
      droppedRef: torn === undefined ? null : this.#artifacts.put(torn.bytes),
    };
    const stored = atomically(this.#db, () => {
      const imported = importSegments(this.#db, log.value, target);
      return imported.ok ? indexImported(this, log.value) : imported;
    });
    return stored.ok ? log : stored;
  }

  /**
   * Takes the branch lease: free or expired, else `branch_busy`. The new epoch is one above
   * both the old lease and every epoch on the resolved chain (wire rule 11). A branch imported
   * with a torn tail records log_repaired under the new lease and becomes runnable.
   */
  acquire(
    branchId: BranchId,
    holderId: string,
    ttlMs: number = LEASE_TTL_MS,
  ): Result<Writer, LogError> {
    return atomically(this.#db, () => {
      const row = ownedBranch(this.#db, branchId, this.tenant);
      const torn = row.ok && isTorn(row.value) ? row.value : undefined;
      if (torn !== undefined) markRepaired(this.#db, branchId);
      const log = this.#runnable(branchId);
      if (!log.ok) return log;
      const writer = takeLease(
        this.#access,
        branchId,
        holderId,
        ttlMs,
        log.value,
      );
      if (!writer.ok || torn === undefined) return writer;
      return recordRepair(writer.value, this.#artifacts, torn, log.value);
    });
  }

  /**
   * After a crash mid-fork the fork's creator owns cleanup: it retakes the
   * `forking` branch's lease, like `acquire`, to finish the fork or fail it and release what
   * the ledger recorded.
   */
  reclaimFork(
    branchId: BranchId,
    holderId: string,
    ttlMs: number = LEASE_TTL_MS,
  ): Result<Writer, LogError> {
    return reclaimFork(this.#access, branchId, holderId, ttlMs);
  }

  /**
   * The chain of a branch this implementation may write: this tenant's, `ready`, verified, and
   * headed by this implementation at this major version.
   */
  #runnable(branchId: BranchId): Result<VerifiedLog, LogError> {
    const branch = ownedBranch(this.#db, branchId, this.tenant);
    if (!branch.ok) return branch;
    const state: BranchRow["state"] = branch.value.state;
    if (state !== "ready")
      return err(
        logError(
          "branch_not_runnable",
          `branch ${branchId} is ${state}`,
          branch.value.head_seq,
        ),
      );
    const log = this.read(branchId);
    if (!log.ok)
      return err(logError("log_corrupt", log.error.message, log.error.seq));
    const mismatch = writerMismatch(log.value);
    return mismatch === undefined ? log : err(mismatch);
  }

  /**
   * Starts a child branch at an eligible snapshot (steps 1 and 3): its row in
   * state `forking` with its own header, and its lease, one epoch above the parent's chain. A
   * forking branch is neither listed nor runnable until `finishFork`. Returns the child's writer,
   * which fences the child's resource ledger rows.
   */
  beginFork(
    request: ForkRequest,
    ttlMs: number = LEASE_TTL_MS,
  ): Result<Writer, LogError> {
    return beginFork(this.#access, request, ttlMs);
  }

  /** Step 4, in one transaction: the child's fork event bound to the parent's line, then ready. */
  finishFork(
    writer: Writer,
    restored: {
      readonly sandboxId: SandboxId;
      readonly knowledgePolicy: "pinned" | "current";
    },
  ): Result<void, LogError> {
    return finishFork(this.#access, writer, restored);
  }

  /** A fork that can't finish: the branch becomes `fork_failed`, never listed or runnable. */
  failFork(writer: Writer): Result<void, LogError> {
    return writer.fenced(() => {
      setBranchState(this.#db, writer.lease.branchId, "fork_failed");
      return ok(undefined);
    });
  }

  branches(id: ThreadId): Result<readonly forking.ListedBranch[], LogError> {
    return forking.listedBranches(this.#db, id, this.tenant);
  }

  /** Branches a crash left mid-fork, for their creator's recovery. */
  forkingBranches(): Result<readonly BranchId[], LogError> {
    return forking.forkingBranches(this.#db, this.tenant);
  }

  /** The resource ledger, fenced by the owner's writer. */
  get ledger(): ResourceLedger {
    return new ResourceLedger(this.#db, this.#now, this.tenant);
  }

  /** The tree-wide budget ledger. */
  get budgets(): BudgetLedger {
    return new BudgetLedger(this.#db);
  }

  /** Durable observer cursors. */
  get cursors(): ObserverCursors {
    return new ObserverCursors(this.#db);
  }

  /** Host-issued memory and knowledge bindings and their audit. */
  get bindings(): HostBindings {
    return new HostBindings(this.#db, this.#now);
  }

  /** The connection, for core's built-in providers' private tables (localMemory, localKnowledge). */
  get driver(): SqliteDriver {
    return this.#db;
  }

  /** What the lease and fork steps (lease.ts, fork-writes.ts) use of this store. */
  get #access(): StoreAccess {
    return {
      db: this.#db,
      now: this.#now,
      tenant: this.tenant,
      read: (branchId) => this.read(branchId),
    };
  }
}
