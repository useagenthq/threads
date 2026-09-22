import { sha256Hex } from "../hash";
import type { BranchId, SandboxId, ThreadId } from "../log";
import { err, ok, type Result } from "../result";
import {
  addLine,
  type Chain,
  emptyChain,
  tipHash,
  type VerifiedLog,
  verifyExport,
  verifyLines,
} from "../verify";
import { type LogError, logError } from "../verify/error";
import { VERSION } from "../version";
import type { SqliteDriver } from "./driver";
import { canonicalLine } from "./encode";
import { importSegments } from "./import";
import { branchLines, exportBytes, headLine } from "./lines";
import {
  atomically,
  DDL,
  getBranch,
  getLease,
  insertBranch,
  putLease,
} from "./tables";
import { Writer } from "./writer";

/** 30 s lease TTL, renewed every 10 s by the holder. */
export const LEASE_TTL_MS = 30_000;

export type ForkRequest = {
  readonly parent: BranchId;
  readonly atSeq: number;
  readonly branch: BranchId;
  readonly holderId: string;
  readonly sandboxId: SandboxId;
  readonly knowledgePolicy: "pinned" | "current";
};

/**
 * The append-only log store on SQLite: exact line bytes, a head checkpoint per
 * branch updated with every append, one fenced writer per branch, and forks that reference
 * their parent's rows. Every read goes back through the import checks.
 */
export class LogStore {
  readonly #db: SqliteDriver;
  readonly #now: () => number;

  /** `now` is the injected clock for leases, event times and snapshot expiry. */
  constructor(db: SqliteDriver, now: () => number) {
    db.exec(DDL);
    this.#db = db;
    this.#now = now;
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
        parent_branch_id: null,
        fork_at_seq: null,
        header_line: header.value,
        state: "ready",
        head_seq: 0,
        head_hash: sha256Hex(header.value),
      });
      return ok(undefined);
    });
  }

  /** The branch's verified resolved chain. Storage is a trust boundary, so this re-verifies. */
  read(branchId: BranchId): Result<VerifiedLog, LogError> {
    const branch = branchLines(this.#db, branchId);
    if (!branch.ok) return branch;
    const head = headLine(branch.value.row);
    if (!head.ok) return head;
    return verifyLines([...branch.value.lines, head.value]);
  }

  /** `threads export`: ancestor segments, the branch's lines, then its head line. */
  exportBranch(branchId: BranchId): Result<Uint8Array, LogError> {
    return exportBytes(this.#db, branchId);
  }

  /** `threads import`: verifies the export, then stores the same bytes, segment by segment. */
  importLog(bytes: Uint8Array): Result<VerifiedLog, LogError> {
    const log = verifyExport(bytes);
    if (!log.ok) return log;
    const stored = atomically(this.#db, () =>
      importSegments(this.#db, log.value),
    );
    return stored.ok ? log : stored;
  }

  /**
   * Takes the branch lease: free or expired, else `branch_busy`. The new epoch is one above
   * both the old lease and every epoch on the resolved chain (wire rule 11).
   */
  acquire(
    branchId: BranchId,
    holderId: string,
    ttlMs: number = LEASE_TTL_MS,
  ): Result<Writer, LogError> {
    return atomically(this.#db, () => {
      const now = this.#now();
      const branch = getBranch(this.#db, branchId);
      if (!branch.ok) return branch;
      const state = branch.value?.state;
      if (state !== "ready")
        return err(
          logError(
            "branch_not_runnable",
            `branch ${branchId} is ${state ?? "absent"}`,
          ),
        );
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
      const log = this.read(branchId);
      if (!log.ok)
        return err(logError("log_corrupt", log.error.message, log.error.seq));
      const epoch = Math.max(held?.epoch ?? 0, log.value.fold.epoch) + 1;
      return ok(this.#lease(branchId, holderId, epoch, now + ttlMs, log.value));
    });
  }

  /**
   * Creates a child branch at an eligible snapshot: its own header,
   * then its fork event bound to the parent's line. The sandbox restore and resource ledger
   * belong to the caller. Returns the child's writer.
   */
  fork(
    request: ForkRequest,
    ttlMs: number = LEASE_TTL_MS,
  ): Result<Writer, LogError> {
    return atomically(this.#db, () => {
      const parent = this.read(request.parent);
      if (!parent.ok) return parent;
      const eligible = this.#eligible(parent.value, request.atSeq);
      if (!eligible.ok) return eligible;
      const opened = this.#openChild(parent.value, request);
      if (!opened.ok) return opened;
      const epoch = opened.value.fold.epoch + 1;
      const writer = this.#lease(
        request.branch,
        request.holderId,
        epoch,
        this.#now() + ttlMs,
        opened.value,
      );
      const forked = writer.append([
        {
          type: "fork",
          type_version: 1,
          critical: true,
          actor: { kind: "host" },
          data: {
            parent_branch_id: request.parent,
            at_hash: tipHash(opened.value) ?? "",
            reason: "snapshot",
            sandbox_id: request.sandboxId,
            knowledge_policy: request.knowledgePolicy,
          },
        },
      ]);
      return forked.ok ? ok(writer) : forked;
    });
  }

  #eligible(parent: Chain, atSeq: number): Result<void, LogError> {
    const snapshot = parent.fold.snapshots.find((s) => s.seq === atSeq);
    if (snapshot === undefined || !snapshot.quiescent)
      return err(
        logError(
          "no_snapshot_boundary",
          `seq ${atSeq} is not a quiescent snapshot`,
          atSeq,
        ),
      );
    const expired =
      snapshot.expiresAt !== null && snapshot.expiresAt <= this.#now();
    return expired
      ? err(
          logError(
            "snapshot_expired",
            `the snapshot at seq ${atSeq} has expired`,
            atSeq,
          ),
        )
      : ok(undefined);
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
      parent_branch_id: request.parent,
      fork_at_seq: request.atSeq,
      header_line: header.value,
      state: "ready",
      head_seq: request.atSeq,
      head_hash: sha256Hex(header.value),
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
      writer: { impl: "threads-ts", version: VERSION },
    });
  }
}
