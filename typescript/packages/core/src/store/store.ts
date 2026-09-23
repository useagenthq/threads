import { sha256Hex } from "../hash";
import type { BranchId, SandboxId, ThreadId } from "../log";
import { knownEvents } from "../reduce";
import { refReader, verifyRequests } from "../render";
import { err, ok, type Result } from "../result";
import {
  addLine,
  type Chain,
  emptyChain,
  tipHash,
  type VerifiedLog,
  verifyExport,
} from "../verify";
import { type LogError, logError } from "../verify/error";
import { VERSION } from "../version";
import type { ArtifactStore } from "./artifacts";
import type { SqliteDriver } from "./driver";
import { canonicalLine } from "./encode";
import { forkEligible } from "./forking";
import { importSegments } from "./import";
import { ResourceLedger } from "./ledger";
import { branchLines, exportBytes } from "./lines";
import { isTorn, markRepaired, recordRepair } from "./repair";
import {
  atomically,
  type BranchRow,
  getBranch,
  getLease,
  insertBranch,
  installSchema,
  LOCAL_TENANT,
  ownedBranch,
  putLease,
  rootBranch,
  setBranchState,
} from "./tables";
import { IMPL, Writer, writerMismatch } from "./writer";

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
    return atomically(this.#db, () => {
      const exists = this.#absent(branchId);
      if (!exists.ok) return exists;
      const header = this.#header(threadId, branchId);
      if (!header.ok) return header;
      insertBranch(this.#db, {
        branch_id: branchId,
        thread_id: threadId,
        tenant_id: this.#tenant,
        parent_branch_id: null,
        fork_at_seq: null,
        header_line: header.value,
        state: "ready",
        head_seq: 0,
        head_hash: sha256Hex(header.value),
        head_verified: 1,
        dropped_ref: null,
      });
      return ok(undefined);
    });
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
      const lease = getLease(this.#db, branchId);
      if (!lease.ok) return lease;
      const held = lease.value;
      if (
        held !== undefined &&
        held.expires_at > now &&
        held.holder_id !== holderId
      )
        return err(
          logError("branch_busy", `branch ${branchId} has a live lease`),
        );
      const epoch = Math.max(held?.epoch ?? 0, log.value.fold.epoch) + 1;
      const writer = this.#lease(
        branchId,
        holderId,
        epoch,
        now + ttlMs,
        log.value,
      );
      return torn === undefined
        ? ok(writer)
        : recordRepair(writer, this.#artifacts, torn, log.value);
    });
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

  /** The resource ledger, fenced by the owner's writer. */
  get ledger(): ResourceLedger {
    return new ResourceLedger(this.#db, this.#now, this.#tenant);
  }

  /** Stores the child's row and header, and loads its chain: the parent's through at_seq. */
  #openChild(
    parent: VerifiedLog,
    request: ForkRequest,
  ): Result<Chain, LogError> {
    const exists = this.#absent(request.branch);
    if (!exists.ok) return exists;
    const threadId = parent.segments[0]?.header.thread_id;
    if (threadId === undefined) throw new Error("a verified log has a header");
    const header = this.#header(threadId, request.branch);
    if (!header.ok) return header;
    insertBranch(this.#db, {
      branch_id: request.branch,
      thread_id: threadId,
      tenant_id: this.#tenant,
      parent_branch_id: request.parent,
      fork_at_seq: request.atSeq,
      header_line: header.value,
      state: "forking",
      head_seq: request.atSeq,
      head_hash: sha256Hex(header.value),
      head_verified: 1,
      dropped_ref: null,
    });
    const lines = branchLines(this.#db, request.branch);
    if (!lines.ok) return lines;
    const chain = emptyChain();
    for (const line of lines.value.lines) {
      const added = addLine(chain, line);
      if (!added.ok) return added;
    }
    return ok(chain);
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

  #absent(branchId: string): Result<void, LogError> {
    const row = getBranch(this.#db, branchId);
    if (!row.ok) return row;
    return row.value === undefined
      ? ok(undefined)
      : err(
          logError("invalid_transition", `branch ${branchId} already exists`),
        );
  }

  #header(threadId: string, branchId: string): Result<Uint8Array, LogError> {
    return canonicalLine({
      format: "threads.log",
      format_version: 1,
      thread_id: threadId,
      branch_id: branchId,
      created_at: this.#now(),
      writer: { impl: IMPL, version: VERSION },
    });
  }
}
