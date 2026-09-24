import { err, ok, type Result } from "../result";
import type { Chain, ChainEvent, VerifiedLog } from "../verify";
import { type LogError, logError } from "../verify/error";
import { VERSION } from "../version";
import {
  type Admitted,
  admitDrafts,
  copyChain,
  type EventDraft,
} from "./admit";
import { recordApprovals } from "./approvals";
import type { SqliteDriver } from "./driver";
import { indexAppend, knownOf } from "./indexing";
import {
  atomically,
  type BranchRow,
  getBranch,
  getLease,
  insertEvents,
  putLease,
} from "./tables";

export type { EventDraft } from "./admit";

export type Lease = {
  readonly branchId: string;
  readonly holderId: string;
  readonly epoch: number;
  /** How long the lease lasts from each take or renewal. */
  readonly ttlMs: number;
};

/** What a decided append's `decide` reads, inside the append's transaction. */
export type DecideTx = {
  readonly db: SqliteDriver;
  /** The committed chain the drafts extend: the stored head is its head. */
  readonly chain: Chain;
  /** The append's clock: every event's time. */
  readonly now: number;
};

/** A decided append whose decision refused: nothing was appended, and the writer goes on. */
export type Refusal<E> = { readonly refused: E };

/** The `writer.impl` this implementation writes in every header it creates. */
export const IMPL = "threads-ts";

const major = (version: string): string => version.split(".")[0] ?? "";

/**
 * Only the implementation a branch's header names, at the same major version, appends to it
 *. Anyone may read, export or fork it.
 */
export function writerMismatch(log: VerifiedLog): LogError | undefined {
  const writer = log.segments.at(-1)?.header.writer;
  if (writer?.impl === IMPL && major(writer.version) === major(VERSION))
    return undefined;
  const named = `${writer?.impl} ${writer?.version}`;
  return logError(
    "writer_mismatch",
    `${named} writes this branch; continue on a fork`,
    0,
  );
}

// Rolls a refused decision back; its code never poisons the writer.
const REFUSED: LogError = logError("invalid_request", "the decision refused");

/**
 * The single writer of one branch under one lease epoch. Every append runs the
 * same admission as import, then commits only if the lease is still this one and the stored
 * head is where this writer left it. A stale lease or a moved head poisons the writer.
 */
export class Writer {
  readonly #db: SqliteDriver;
  readonly #now: () => number;
  readonly lease: Lease;
  /**
   * The chain had a pending tool call, a model request awaiting its response, or an effect
   * `begun` or `unknown` when this lease was taken. Such a branch is in doubt: normal dispatch must refuse this writer, and only
   * recovery may settle and continue it.
   */
  readonly requiresRecovery: boolean;
  #chain: Chain;
  #poisoned = false;
  #moved = Promise.withResolvers<void>();

  constructor(db: SqliteDriver, now: () => number, lease: Lease, chain: Chain) {
    this.#db = db;
    this.#now = now;
    this.lease = lease;
    this.#chain = chain;
    const { pending, awaiting, effects } = chain.fold;
    this.requiresRecovery =
      pending.size > 0 ||
      awaiting.size > 0 ||
      effects
        .values()
        .some((e) => e.status === "begun" || e.status === "unknown");
  }

  /** Settles on this writer's next committed append, whoever made it (a control included). */
  moved(): Promise<void> {
    return this.#moved.promise;
  }

  /** The committed chain this writer appends to. Each append replaces it; none mutates it. */
  get chain(): Chain {
    return this.#chain;
  }

  /**
   * Appends drafts in order and returns once their transaction has committed with full sync.
   * `alongside` writes host rows (an idempotency receipt, a consumed inbox item) in the same
   * transaction, after the events and their index rows; an error from it rolls the append back.
   */
  append(
    drafts: readonly EventDraft[],
    alongside?: (added: readonly ChainEvent[]) => Result<void, LogError>,
  ): Result<readonly ChainEvent[], LogError> {
    return this.#run(() => ok(drafts), alongside);
  }

  /**
   * One transaction that begins, checks the lease and head, lets `decide` read the store and
   * build the drafts, admits them and commits. A refusal rolls back and leaves the writer
   * usable; `stale_epoch` and `seq_conflict` poison it, as for `append`.
   */
  appendDecided<E>(
    decide: (tx: DecideTx) => Result<readonly EventDraft[], E>,
  ): Result<readonly ChainEvent[], LogError | Refusal<E>> {
    const outcome: { refusal?: Refusal<E> } = {};
    const appended = this.#run((tx) => {
      const decided = decide(tx);
      if (decided.ok) return decided;
      outcome.refusal = { refused: decided.error };
      return err(REFUSED);
    });
    return outcome.refusal === undefined ? appended : err(outcome.refusal);
  }

  #run(
    produce: (tx: DecideTx) => Result<readonly EventDraft[], LogError>,
    alongside?: (added: readonly ChainEvent[]) => Result<void, LogError>,
  ): Result<readonly ChainEvent[], LogError> {
    if (this.#poisoned)
      return err(
        logError("writer_poisoned", "this writer lost its lease or head"),
      );
    const trial = copyChain(this.#chain);
    const now = this.#now();
    const committed = atomically(this.#db, () => {
      const branch = this.#fencedHead();
      if (!branch.ok) return branch;
      const drafts = produce({ db: this.#db, chain: this.#chain, now });
      if (!drafts.ok) return drafts;
      const admitted = admitDrafts(trial, drafts.value, now, this.lease.epoch);
      if (!admitted.ok) return admitted;
      const written = this.#write(branch.value, admitted.value, now);
      if (!written.ok || alongside === undefined)
        return written.ok ? ok(admitted.value.events) : written;
      const host = alongside(admitted.value.events);
      return host.ok ? ok(admitted.value.events) : host;
    });
    if (!committed.ok) {
      const { code } = committed.error;
      this.#poisoned = code === "stale_epoch" || code === "seq_conflict";
      return committed;
    }
    this.#chain = trial;
    this.#moved.resolve();
    this.#moved = Promise.withResolvers<void>();
    return committed;
  }

  /**
   * The dispatch fence: this writer still holds the lease at its epoch, read
   * from the store right before an adapter is called. A stale writer is poisoned and must never
   * dispatch. Passing proves nothing about what an older owner already sent.
   */
  fence(): Result<void, LogError> {
    if (this.#poisoned)
      return err(
        logError("writer_poisoned", "this writer lost its lease or head"),
      );
    const live = this.#checkLease();
    if (!live.ok) this.#poisoned = true;
    return live;
  }

  /**
   * Runs `fn` in one transaction that first checks this lease, so a store write outside the log
   * (a resource ledger row) is fenced like an append. A stale writer is poisoned.
   */
  fenced<T>(fn: () => Result<T, LogError>): Result<T, LogError> {
    if (this.#poisoned)
      return err(
        logError("writer_poisoned", "this writer lost its lease or head"),
      );
    const done = atomically(this.#db, () => {
      const live = this.#checkLease();
      return live.ok ? fn() : live;
    });
    if (!done.ok && done.error.code === "stale_epoch") this.#poisoned = true;
    return done;
  }

  /** Extends the lease; fails (and poisons) if another holder or epoch took it or it lapsed. */
  renew(ttlMs: number): Result<void, LogError> {
    if (this.#poisoned)
      return err(
        logError("writer_poisoned", "this writer lost its lease or head"),
      );
    const renewed = atomically(this.#db, () => {
      const live = this.#checkLease();
      if (!live.ok) return live;
      putLease(this.#db, this.lease.branchId, {
        holder_id: this.lease.holderId,
        epoch: this.lease.epoch,
        expires_at: this.#now() + ttlMs,
      });
      return ok(undefined);
    });
    if (!renewed.ok) this.#poisoned = true;
    return renewed;
  }

  /**
   * Hands the lease back so the next executor can take the branch at once. Only while this
   * holder and epoch still hold it: a stale writer never clears a newer owner's lease. Only the
   * lease changes; in-doubt work stays in the log for recovery. The writer is done afterwards.
   */
  release(): void {
    if (!this.#poisoned)
      atomically(this.#db, () => {
        if (this.#checkLease().ok)
          putLease(this.#db, this.lease.branchId, {
            holder_id: this.lease.holderId,
            epoch: this.lease.epoch,
            expires_at: this.#now(),
          });
        return ok(undefined);
      });
    this.#poisoned = true;
  }

  /** The lease is still this one and the stored head is where this writer left it. */
  #fencedHead(): Result<BranchRow, LogError> {
    const live = this.#checkLease();
    if (!live.ok) return live;
    const branch = getBranch(this.#db, this.lease.branchId);
    if (!branch.ok) return branch;
    const expected = this.#chain.fold.seq;
    if (branch.value === undefined || branch.value.head_seq !== expected)
      return err(
        logError("seq_conflict", `the stored head is not ${expected}`),
      );
    return ok(branch.value);
  }

  /** The admitted events, their index rows and their approvals' challenge rows. */
  #write(
    branch: BranchRow,
    admitted: Admitted,
    now: number,
  ): Result<void, LogError> {
    const { events, opened } = admitted;
    insertEvents(this.#db, this.lease.branchId, events);
    const own = this.#chain.segments.at(-1)?.header;
    if (own === undefined) throw new Error("a writer's chain has a header");
    const indexed = indexAppend({
      db: this.#db,
      tenant: branch.tenant_id,
      threadId: own.thread_id,
      branchId: own.branch_id,
      events: knownOf(events),
      opened,
      holderId: this.lease.holderId,
      now,
    });
    if (!indexed.ok) return indexed;
    return recordApprovals(this.#db, branch, events, now);
  }

  #checkLease(): Result<void, LogError> {
    const lease = getLease(this.#db, this.lease.branchId);
    if (!lease.ok) return lease;
    const row = lease.value;
    const current =
      row !== undefined &&
      row.holder_id === this.lease.holderId &&
      row.epoch === this.lease.epoch &&
      row.expires_at > this.#now();
    return current
      ? ok(undefined)
      : err(
          logError(
            "stale_epoch",
            `epoch ${this.lease.epoch} no longer holds the lease`,
          ),
        );
  }
}
