import type { BranchId, SandboxId, ThreadId } from "../log";
import { knownEvents } from "../reduce";
import { refReader, verifyRequests } from "../render";
import { err, ok, type Result } from "../result";
import { type Chain, tipHash, type VerifiedLog, verifyExport } from "../verify";
import { type LogError, logError } from "../verify/error";
import type { ArtifactStore } from "./artifacts";
import { HostBindings } from "./bindings";
import { newBranch } from "./branch";
import { BudgetLedger } from "./budget";
import { ObserverCursors } from "./cursors";
import type { SqliteDriver } from "./driver";
import {
  forkEligible,
  forkingBranches,
  type ListedBranch,
  listedBranches,
  loadChain,
} from "./forking";
import { importSegments } from "./import";
import { ResourceLedger } from "./ledger";
import { exportBytes } from "./lines";
import { isTorn, markRepaired, recordRepair } from "./repair";
import {
  atomically,
  type BranchRow,
  getLease,
  installSchema,
  LOCAL_TENANT,
  ownedBranch,
  putLease,
  rootBranch,
  setBranchState,
} from "./tables";
import { Writer, writerMismatch } from "./writer";

/** 30 s lease TTL, renewed every 10 s by the holder. */
export const LEASE_TTL_MS = 30_000;

export type ForkRequest = {
  readonly parent: BranchId;
  readonly atSeq: number;
  readonly branch: BranchId;
  readonly holderId: string;
};

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
  readonly #tenant: string;

  private constructor(
    db: SqliteDriver,
    now: () => number,
    artifacts: ArtifactStore,
    tenantId: string,
  ) {
    this.#db = db;
    this.#now = now;
    this.#artifacts = artifacts;
    this.#tenant = tenantId;
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
    return atomically(this.#db, () =>
      newBranch(this.#db, {
        tenantId: this.#tenant,
        threadId,
        branchId,
        parent: null,
        state: "ready",
        createdAt: this.#now(),
      }),
    );
  }

  /** A branch's state, if this tenant owns it (a forking or failed branch is never listed). */
  branchState(branchId: BranchId): Result<BranchRow["state"], LogError> {
    const row = ownedBranch(this.#db, branchId, this.#tenant);
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
    const root = rootBranch(this.#db, threadId, this.#tenant);
    if (!root.ok) return root;
    return root.value === undefined
      ? err(logError("branch_not_found", `no thread ${threadId}`))
      : ok(root.value);
  }

  /** `threads export`: ancestor segments, the branch's lines, then its head line. */
  exportBranch(branchId: BranchId): Result<Uint8Array, LogError> {
    const owned = ownedBranch(this.#db, branchId, this.#tenant);
    if (!owned.ok) return owned;
    return exportBytes(this.#db, this.#artifacts, branchId);
  }

  /**
   * `threads import`: verifies the export, then stores the same bytes, segment by segment. A
   * torn tail's bytes are kept as an artifact, durable before the rows that name them. Every
   * model request must replay from the log and the artifacts already stored (C7, Render v1).
   */
  importLog(bytes: Uint8Array): Result<VerifiedLog, LogError> {
    const log = verifyExport(bytes);
    if (!log.ok) return log;
    const replayed = verifyRequests(
      knownEvents(log.value),
      refReader(this.#artifacts),
    );
    if (!replayed.ok) return replayed;
    const torn = log.value.torn;
    const target = {
      tenantId: this.#tenant,
      droppedRef: torn === undefined ? null : this.#artifacts.put(torn.bytes),
    };
    const stored = atomically(this.#db, () =>
      importSegments(this.#db, log.value, target),
    );
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
      const now = this.#now();
      const row = ownedBranch(this.#db, branchId, this.#tenant);
      const torn = row.ok && isTorn(row.value) ? row.value : undefined;
      if (torn !== undefined) markRepaired(this.#db, branchId);
      const log = this.#runnable(branchId);
      if (!log.ok) return log;
      const writer = this.#take(branchId, holderId, now + ttlMs, log.value);
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
    return atomically(this.#db, () => {
      const row = ownedBranch(this.#db, branchId, this.#tenant);
      if (!row.ok) return row;
      if (row.value.state !== "forking")
        return err(
          logError("branch_not_runnable", `branch ${branchId} is not forking`),
        );
      const chain = loadChain(this.#db, branchId);
      if (!chain.ok) return chain;
      return this.#take(branchId, holderId, this.#now() + ttlMs, chain.value);
    });
  }

  /** The lease if it is free, expired or already this holder's, at the next epoch (rule 11). */
  #take(
    branchId: BranchId,
    holderId: string,
    expiresAt: number,
    chain: Chain,
  ): Result<Writer, LogError> {
    const lease = getLease(this.#db, branchId);
    if (!lease.ok) return lease;
    const held = lease.value;
    if (
      held !== undefined &&
      held.expires_at > this.#now() &&
      held.holder_id !== holderId
    )
      return err(
        logError("branch_busy", `branch ${branchId} has a live lease`),
      );
    const epoch = Math.max(held?.epoch ?? 0, chain.fold.epoch) + 1;
    return ok(this.#lease(branchId, holderId, epoch, expiresAt, chain));
  }

  /**
   * The chain of a branch this implementation may write: this tenant's, `ready`, verified, and
   * headed by this implementation at this major version.
   */
  #runnable(branchId: BranchId): Result<VerifiedLog, LogError> {
    const branch = ownedBranch(this.#db, branchId, this.#tenant);
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
   * Starts a child branch at an eligible snapshot: its row in
   * state `forking` with its own header, and its lease, one epoch above the parent's chain. A
   * forking branch is neither listed nor runnable until `finishFork`. Returns the child's writer,
   * which fences the child's resource ledger rows.
   */
  beginFork(
    request: ForkRequest,
    ttlMs: number = LEASE_TTL_MS,
  ): Result<Writer, LogError> {
    return atomically(this.#db, () => {
      const owned = ownedBranch(this.#db, request.parent, this.#tenant);
      if (!owned.ok) return owned;
      const parent = this.read(request.parent);
      if (!parent.ok) return parent;
      const eligible = forkEligible(
        parent.value.fold,
        request.atSeq,
        this.#now(),
      );
      if (!eligible.ok) return eligible;
      const opened = this.#openChild(parent.value, request);
      if (!opened.ok) return opened;
      return ok(
        this.#lease(
          request.branch,
          request.holderId,
          opened.value.fold.epoch + 1,
          this.#now() + ttlMs,
          opened.value,
        ),
      );
    });
  }

  /** Step 4, in one transaction: the child's fork event bound to the parent's line, then ready. */
  finishFork(
    writer: Writer,
    restored: {
      readonly sandboxId: SandboxId;
      readonly knowledgePolicy: "pinned" | "current";
    },
  ): Result<void, LogError> {
    const parent = writer.chain.segments.at(-2)?.header.branch_id;
    if (parent === undefined) throw new Error("a forking chain has a parent");
    return atomically(this.#db, () => {
      const forked = writer.append([
        {
          type: "fork",
          type_version: 1,
          critical: true,
          actor: { kind: "host" },
          data: {
            parent_branch_id: parent,
            at_hash: tipHash(writer.chain) ?? "",
            reason: "snapshot",
            sandbox_id: restored.sandboxId,
            knowledge_policy: restored.knowledgePolicy,
          },
        },
      ]);
      if (!forked.ok) return forked;
      setBranchState(this.#db, writer.lease.branchId, "ready");
      // The fork is done with the child; hand the lease back so a run can take it at once.
      putLease(this.#db, writer.lease.branchId, {
        holder_id: writer.lease.holderId,
        epoch: writer.lease.epoch,
        expires_at: this.#now(),
      });
      return ok(undefined);
    });
  }

  /** A fork that can't finish: the branch becomes `fork_failed`, never listed or runnable. */
  failFork(writer: Writer): Result<void, LogError> {
    return writer.fenced(() => {
      setBranchState(this.#db, writer.lease.branchId, "fork_failed");
      return ok(undefined);
    });
  }

  get tenant(): string {
    return this.#tenant;
  }

  branches(threadId: ThreadId): Result<readonly ListedBranch[], LogError> {
    return listedBranches(this.#db, threadId, this.#tenant);
  }

  /** Branches a crash left mid-fork, for their creator's recovery. */
  forkingBranches(): Result<readonly BranchId[], LogError> {
    return forkingBranches(this.#db, this.#tenant);
  }

  /** The resource ledger, fenced by the owner's writer. */
  get ledger(): ResourceLedger {
    return new ResourceLedger(this.#db, this.#now, this.#tenant);
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

  /** Stores the child's row and header, and loads its chain: the parent's through at_seq. */
  #openChild(
    parent: VerifiedLog,
    request: ForkRequest,
  ): Result<Chain, LogError> {
    const threadId = parent.segments[0]?.header.thread_id;
    if (threadId === undefined) throw new Error("a verified log has a header");
    const stored = newBranch(this.#db, {
      tenantId: this.#tenant,
      threadId,
      branchId: request.branch,
      parent: { branchId: request.parent, atSeq: request.atSeq },
      state: "forking",
      createdAt: this.#now(),
    });
    return stored.ok ? loadChain(this.#db, request.branch) : stored;
  }

  #lease(
    branchId: string,
    holderId: string,
    epoch: number,
    expiresAt: number,
    chain: Chain,
  ): Writer {
    putLease(this.#db, branchId, {
      holder_id: holderId,
      epoch,
      expires_at: expiresAt,
    });
    return new Writer(
      this.#db,
      this.#now,
      { branchId, holderId, epoch },
      chain,
    );
  }
}
