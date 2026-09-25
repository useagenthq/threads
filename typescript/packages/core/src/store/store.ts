import type { BranchId, SandboxId, ThreadId } from "../log";
import { err, ok, type Result } from "../result";
import { indexImported } from "../team/imported";
import { type Chain, type VerifiedLog, verifyExport } from "../verify";
import { type LogError, logError } from "../verify/error";
import { acquire } from "./acquire";
import type { ArtifactStore } from "./artifacts";
import { HostBindings } from "./bindings";
import { newBranch } from "./branch";
import { BudgetLedger } from "./budget";
import { ObserverCursors } from "./cursors";
import { READ_ONLY, type StoreDriver, type Tx } from "./driver";
import * as forking from "./fork-reads";
import {
  beginFork,
  type ForkRequest,
  finishFork,
  reclaimFork,
} from "./fork-writes";
import { importSegments, verifiedImport } from "./import";
import { LEASE_TTL_MS, type StoreAccess } from "./lease";
import { ResourceLedger } from "./ledger";
import { exportBytes } from "./lines";
import { ALREADY_OPEN, type BranchOpening, openBranch } from "./open";
import {
  atomically,
  type BranchRow,
  LOCAL_TENANT,
  ownedBranch,
  rootBranch,
  setBranchState,
} from "./tables";
import { nestedDriver } from "./tx";
import { Writer } from "./writer";

export type { ForkRequest } from "./fork-writes";
export { LEASE_TTL_MS } from "./lease";

/**
 * The append-only log store: exact line bytes, a head checkpoint per branch updated with every
 * append, one fenced writer per branch, and forks that reference their parent's rows. Every read
 * goes back through the import checks. The same code runs on SQLite and on Postgres.
 *
 * A store is bound to one tenant: a branch of any other tenant is `branch_not_found`.
 */
export class LogStore {
  readonly #db: StoreDriver;
  readonly #now: () => number;
  readonly #artifacts: ArtifactStore;
  readonly tenant: string;

  private constructor(
    db: StoreDriver,
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
   * Opens the store on `db` for one tenant, creating the store.sql tables. A database of another
   * schema version is `unsupported_format`. `now` is the injected clock for leases, event times
   * and snapshot expiry; `artifacts` keeps the bytes a torn import dropped.
   */
  static async open(
    db: StoreDriver,
    now: () => number,
    artifacts: ArtifactStore,
    tenantId: string = LOCAL_TENANT,
  ): Promise<Result<LogStore, LogError>> {
    const installed = await db.install();
    return installed.ok
      ? ok(new LogStore(db, now, artifacts, tenantId))
      : installed;
  }

  /**
   * This store inside `tx`, a transaction the caller holds: its reads and writes are savepoints
   * of it, and commit with it. For host steps that run several store operations as one.
   */
  within(tx: Tx): LogStore {
    return new LogStore(
      nestedDriver(tx),
      this.#now,
      this.#artifacts,
      this.tenant,
    );
  }

  /** The same connection bound to another tenant (no install: it was opened already). */
  scoped(tenantId: string): LogStore {
    return new LogStore(this.#db, this.#now, this.#artifacts, tenantId);
  }

  /** Writes a new root branch: its header line, head at seq 0. */
  createBranch(
    threadId: ThreadId,
    branchId: BranchId,
  ): Promise<Result<void, LogError>> {
    return atomically(this.#db, async (tx) => {
      const made = await newBranch(tx, {
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
   * The thread's root branch, else `branchId` created as it, in one transaction: processes racing
   * to start a thread all get the root that stood first.
   */
  rootOrCreate(
    threadId: ThreadId,
    branchId: BranchId,
  ): Promise<Result<BranchId, LogError>> {
    return atomically(this.#db, async (tx) => {
      const root = await rootBranch(tx, threadId, this.tenant);
      if (!root.ok) return root;
      if (root.value !== undefined) return ok(root.value);
      const made = await newBranch(tx, {
        tenantId: this.tenant,
        threadId,
        branchId,
        parent: null,
        state: "ready",
        createdAt: this.#now(),
      });
      return made.ok ? ok(branchId) : made;
    });
  }

  /**
   * `branch.open` in a transaction of its own: a new root branch of this tenant with its first
   * events, held by the returned writer at epoch 1. `already_open` when the branch exists.
   */
  async openBranch(
    opening: Omit<BranchOpening, "tenantId">,
  ): Promise<Result<Writer | typeof ALREADY_OPEN, LogError>> {
    const opened = await this.openBranchChecked(async () => ok(opening));
    if (!opened.ok) return opened;
    if (opened.value === undefined) throw new Error("an opening always opens");
    return ok(opened.value);
  }

  /**
   * `branch.open` after a check in the same transaction: `decide` reads the store and returns
   * the opening, or undefined to commit nothing (materialize's row check). It may run again
   * from the start (see `Tx`).
   */
  async openBranchChecked(
    decide: (
      tx: Tx,
      now: number,
    ) => Promise<Result<Omit<BranchOpening, "tenantId"> | undefined, LogError>>,
  ): Promise<Result<Writer | typeof ALREADY_OPEN | undefined, LogError>> {
    const now = this.#now();
    const opened = await atomically<
      | {
          opening: Omit<BranchOpening, "tenantId">;
          chain: Chain | typeof ALREADY_OPEN;
        }
      | undefined
    >(this.#db, async (tx) => {
      const opening = await decide(tx, now);
      if (!opening.ok) return opening;
      const { value } = opening;
      if (value === undefined) return ok(undefined);
      const chain = await openBranch(tx, now, {
        ...value,
        tenantId: this.tenant,
      });
      return chain.ok ? ok({ opening: value, chain: chain.value }) : chain;
    });
    if (!opened.ok) return opened;
    if (opened.value === undefined) return ok(undefined);
    const { opening, chain } = opened.value;
    if (chain === ALREADY_OPEN) return ok(ALREADY_OPEN);
    const { branchId, lease } = opening;
    return ok(
      new Writer(
        this.#db,
        this.#now,
        { branchId, holderId: lease.holderId, epoch: 1, ttlMs: lease.ttlMs },
        chain,
      ),
    );
  }

  /** A branch's state, if this tenant owns it (a forking or failed branch is never listed). */
  async branchState(
    branchId: BranchId,
  ): Promise<Result<BranchRow["state"], LogError>> {
    const row = await this.#reading((tx) =>
      ownedBranch(tx, branchId, this.tenant),
    );
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
  read(branchId: BranchId): Promise<Result<VerifiedLog, LogError>> {
    return this.#reading((tx) => this.readIn(tx, branchId));
  }

  /** `read` inside a transaction the caller holds. */
  async readIn(
    tx: Tx,
    branchId: BranchId,
  ): Promise<Result<VerifiedLog, LogError>> {
    const bytes = await this.#export(tx, branchId);
    return bytes.ok ? verifyExport(bytes.value) : bytes;
  }

  /** A thread's main branch, or `branch_not_found`. */
  async mainBranch(threadId: ThreadId): Promise<Result<BranchId, LogError>> {
    const root = await this.#reading((tx) =>
      rootBranch(tx, threadId, this.tenant),
    );
    if (!root.ok) return root;
    return root.value === undefined
      ? err(logError("branch_not_found", `no thread ${threadId}`))
      : ok(root.value);
  }

  /** `threads export`: ancestor segments, the branch's lines, then its head line. */
  exportBranch(branchId: BranchId): Promise<Result<Uint8Array, LogError>> {
    return this.#reading((tx) => this.#export(tx, branchId));
  }

  async #export(
    tx: Tx,
    branchId: BranchId,
  ): Promise<Result<Uint8Array, LogError>> {
    const owned = await ownedBranch(tx, branchId, this.tenant);
    if (!owned.ok) return owned;
    return exportBytes(tx, this.#artifacts, branchId);
  }

  /**
   * `threads import`: verifies the export, then stores the same bytes, segment by segment. A
   * torn tail's bytes are kept as an artifact, durable before the rows that name them. Every
   * model request must replay from the log and the artifacts already stored (C7, Render v1).
   * The index rows the log holds (wake rows, its teams) are folded again in the same
   * transaction.
   */
  async importLog(bytes: Uint8Array): Promise<Result<VerifiedLog, LogError>> {
    const log = await verifiedImport(bytes, this.#artifacts);
    if (!log.ok) return log;
    const torn = log.value.torn;
    const target = {
      tenantId: this.tenant,
      droppedRef:
        torn === undefined ? null : await this.#artifacts.put(torn.bytes),
    };
    const stored = await atomically(this.#db, async (tx) => {
      const imported = await importSegments(tx, log.value, target);
      return imported.ok ? indexImported(tx, this, log.value) : imported;
    });
    return stored.ok ? log : stored;
  }

  /**
   * Takes the branch lease: free or expired, else `branch_busy` (acquire.ts). A torn import
   * records log_repaired under the new lease and becomes runnable.
   */
  acquire(
    branchId: BranchId,
    holderId: string,
    ttlMs: number = LEASE_TTL_MS,
  ): Promise<Result<Writer, LogError>> {
    return acquire(this.#access, this.#artifacts, branchId, holderId, ttlMs);
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
  ): Promise<Result<Writer, LogError>> {
    return reclaimFork(this.#access, branchId, holderId, ttlMs);
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
  ): Promise<Result<Writer, LogError>> {
    return beginFork(this.#access, request, ttlMs);
  }

  /** Step 4, in one transaction: the child's fork event bound to the parent's line, then ready. */
  finishFork(
    writer: Writer,
    restored: {
      readonly sandboxId: SandboxId;
      readonly knowledgePolicy: "pinned" | "current";
    },
  ): Promise<Result<void, LogError>> {
    return finishFork(this.#access, writer, restored);
  }

  /** A fork that can't finish: the branch becomes `fork_failed`, never listed or runnable. */
  failFork(writer: Writer): Promise<Result<void, LogError>> {
    return writer.fenced(async (tx) => {
      await setBranchState(tx, writer.lease.branchId, "fork_failed");
      return ok(undefined);
    });
  }

  branches(
    id: ThreadId,
  ): Promise<Result<readonly forking.ListedBranch[], LogError>> {
    return this.#reading((tx) => forking.listedBranches(tx, id, this.tenant));
  }

  /** Branches a crash left mid-fork, for their creator's recovery. */
  forkingBranches(): Promise<Result<readonly BranchId[], LogError>> {
    return this.#reading((tx) => forking.forkingBranches(tx, this.tenant));
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

  /** The connection, for the host's tables and core's built-in providers' private tables. */
  get driver(): StoreDriver {
    return this.#db;
  }

  /** The content-addressed artifacts beside the log. */
  get artifacts(): ArtifactStore {
    return this.#artifacts;
  }

  #reading<T>(fn: (tx: Tx) => Promise<T>): Promise<T> {
    return this.#db.transaction(fn, READ_ONLY);
  }

  /** What the lease and fork steps (lease.ts, fork-writes.ts) use of this store. */
  get #access(): StoreAccess {
    return {
      db: this.#db,
      now: this.#now,
      tenant: this.tenant,
      read: (tx, branchId) => this.readIn(tx, branchId),
    };
  }
}
