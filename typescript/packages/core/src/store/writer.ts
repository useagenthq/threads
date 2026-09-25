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
import { CommitUnknown, READ_ONLY, type StoreDriver, type Tx } from "./driver";
import { indexAppend, knownOf } from "./indexing";
import {
  atomically,
  type BranchRow,
  getBranch,
  getLease,
  insertEvents,
  putLease,
} from "./tables";
import { Fifo } from "./tx";

export type { EventDraft } from "./admit";

export type Lease = {
  readonly branchId: string;
  readonly holderId: string;
  readonly epoch: number;
  /** How long the lease lasts from each take or renewal. */
  readonly ttlMs: number;
};

/**
 * What a decided append's `decide` reads, inside the append's transaction. `decide` may run
 * again from the start (see `Tx`): it reads only through `tx` and returns what it built.
 */
export type DecideTx = {
  readonly tx: Tx;
  /** The committed chain the drafts extend: the stored head is its head. */
  readonly chain: Chain;
  /** The append's clock: every event's time. */
  readonly now: number;
};

/**
 * Host rows written in an append's transaction, after its events and index rows (a receipt, a
 * consumed inbox item). It may run again from the start, and never calls back into the writer.
 */
export type Alongside = (
  added: readonly ChainEvent[],
  tx: Tx,
) => Promise<Result<void, LogError>>;

/** A decided append whose decision refused: nothing was appended, and the writer goes on. */
export type Refusal<E> = { readonly kind: "refused"; readonly refusal: E };

/** Whether a decided append was refused, rather than appended or failed. */
export function isRefusal<T, E>(
  outcome: T | Refusal<E>,
): outcome is Refusal<E> {
  return (
    typeof outcome === "object" &&
    outcome !== null &&
    "kind" in outcome &&
    outcome.kind === "refused"
  );
}

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

const poisoned = (): LogError =>
  logError("writer_poisoned", "this writer lost its lease or head");

type Produce = (
  tx: DecideTx,
) => Promise<Result<readonly EventDraft[], LogError>>;

/**
 * The single writer of one branch under one lease epoch. Every append runs the
 * same admission as import, then commits only if the lease is still this one and the stored
 * head is where this writer left it. A stale lease or a moved head poisons the writer, and so
 * does a commit whose outcome is unknown: the owner reloads the branch from the log.
 */
export class Writer {
  readonly #db: StoreDriver;
  readonly #now: () => number;
  /** One append, fence, renewal or release at a time, each against the committed chain. */
  readonly #lock = new Fifo();
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

  constructor(db: StoreDriver, now: () => number, lease: Lease, chain: Chain) {
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
   * `alongside` writes host rows in the same transaction; an error from it rolls the append back.
   */
  append(
    drafts: readonly EventDraft[],
    alongside?: Alongside,
  ): Promise<Result<readonly ChainEvent[], LogError>> {
    return this.#locked(() =>
      this.#run(this.#db, async () => ok(drafts), alongside),
    );
  }

  /**
   * `append` as a savepoint of `outer`, a transaction the caller holds; the chain moves once
   * `outer` commits. For a writer nobody else appends through yet (a fork's, a repair's).
   */
  appendIn(
    outer: Tx,
    drafts: readonly EventDraft[],
  ): Promise<Result<readonly ChainEvent[], LogError>> {
    return this.#locked(() => this.#run(outer, async () => ok(drafts)));
  }

  /**
   * One transaction that begins, checks the lease and head, lets `decide` read the store and
   * build the drafts, admits them and commits. A refusal rolls back and leaves the writer
   * usable; `stale_epoch` and `seq_conflict` poison it, as for `append`. A batch that comes out
   * empty (the barrier dropped every draft) commits nothing and moves nothing.
   */
  async appendDecided<E>(
    decide: (tx: DecideTx) => Promise<Result<readonly EventDraft[], E>>,
    alongside?: Alongside,
  ): Promise<Result<readonly ChainEvent[], LogError> | Refusal<E>> {
    // Each attempt decides afresh: only the last attempt's refusal counts.
    let refusal: Refusal<E> | undefined;
    const appended = await this.#locked(() =>
      this.#run(
        this.#db,
        async (tx) => {
          refusal = undefined;
          const decided = await decide(tx);
          if (decided.ok) return decided;
          refusal = { kind: "refused", refusal: decided.error };
          return err(REFUSED);
        },
        alongside,
      ),
    );
    return refusal ?? appended;
  }

  async #locked<T>(fn: () => Promise<T>): Promise<T> {
    const release = await this.#lock.turn();
    try {
      return await fn();
    } finally {
      release();
    }
  }

  async #run(
    sql: StoreDriver | Tx,
    produce: Produce,
    alongside?: Alongside,
  ): Promise<Result<readonly ChainEvent[], LogError>> {
    if (this.#poisoned) return err(poisoned());
    const committed = await this.#guard(() =>
      atomically(sql, (tx) => this.#attempt(tx, produce, alongside)),
    );
    if (!committed.ok) {
      const { code } = committed.error;
      this.#poisoned = code === "stale_epoch" || code === "seq_conflict";
    }
    return committed;
  }

  /** One attempt, built from the committed chain, so a retried attempt starts clean. */
  async #attempt(
    tx: Tx,
    produce: Produce,
    alongside: Alongside | undefined,
  ): Promise<Result<readonly ChainEvent[], LogError>> {
    const trial = copyChain(this.#chain);
    const now = this.#now();
    const branch = await this.#fencedHead(tx);
    if (!branch.ok) return branch;
    const drafts = await produce({ tx, chain: this.#chain, now });
    if (!drafts.ok) return drafts;
    const admitted = admitDrafts(trial, drafts.value, now, this.lease.epoch);
    if (!admitted.ok) return admitted;
    const { events } = admitted.value;
    const written = await this.#write(tx, branch.value, admitted.value, now);
    if (!written.ok) return written;
    const host =
      alongside === undefined ? ok(undefined) : await alongside(events, tx);
    if (!host.ok) return host;
    if (events.length > 0) tx.afterCommit(() => this.#publish(trial));
    return ok(events);
  }

  /** A committed append moves the chain and wakes whoever waits on this writer. */
  #publish(trial: Chain): void {
    this.#chain = trial;
    this.#moved.resolve();
    this.#moved = Promise.withResolvers<void>();
  }

  /** A commit whose outcome is unknown poisons the writer: its owner reloads it from the log. */
  async #guard<T>(fn: () => Promise<T>): Promise<T> {
    try {
      return await fn();
    } catch (error) {
      if (error instanceof CommitUnknown) this.#poisoned = true;
      throw error;
    }
  }

  /**
   * The dispatch fence: this writer still holds the lease at its epoch, read
   * from the store right before an adapter is called. A stale writer is poisoned and must never
   * dispatch. Passing proves nothing about what an older owner already sent.
   */
  fence(): Promise<Result<void, LogError>> {
    return this.#locked(async () => {
      if (this.#poisoned) return err(poisoned());
      const live = await this.#db.transaction(
        (tx) => this.#checkLease(tx),
        READ_ONLY,
      );
      if (!live.ok) this.#poisoned = true;
      return live;
    });
  }

  /**
   * Runs `fn` in one transaction that first checks this lease, so a store write outside the log
   * (a resource ledger row) is fenced like an append. A stale writer is poisoned.
   */
  async fenced<T>(
    fn: (tx: Tx) => Promise<Result<T, LogError>>,
  ): Promise<Result<T, LogError>> {
    if (this.#poisoned) return err(poisoned());
    const done = await atomically(this.#db, async (tx) => {
      const live = await this.#checkLease(tx);
      return live.ok ? fn(tx) : live;
    });
    if (!done.ok && done.error.code === "stale_epoch") this.#poisoned = true;
    return done;
  }

  /** Extends the lease; fails (and poisons) if another holder or epoch took it or it lapsed. */
  renew(ttlMs: number): Promise<Result<void, LogError>> {
    return this.#locked(async () => {
      if (this.#poisoned) return err(poisoned());
      const renewed = await this.#guard(() =>
        atomically(this.#db, async (tx) => {
          const live = await this.#checkLease(tx);
          if (!live.ok) return live;
          await this.#putLease(tx, this.#now() + ttlMs);
          return ok(undefined);
        }),
      );
      if (!renewed.ok) this.#poisoned = true;
      return renewed;
    });
  }

  /**
   * Hands the lease back so the next executor can take the branch at once. Only while this
   * holder and epoch still hold it: a stale writer never clears a newer owner's lease. Only the
   * lease changes; in-doubt work stays in the log for recovery. The writer is done afterwards.
   */
  release(): Promise<void> {
    return this.#locked(async () => {
      const live = !this.#poisoned;
      this.#poisoned = true;
      if (!live) return;
      await atomically(this.#db, async (tx) => {
        if ((await this.#checkLease(tx)).ok)
          await this.#putLease(tx, this.#now());
        return ok(undefined);
      });
    });
  }

  #putLease(tx: Tx, expiresAt: number): Promise<void> {
    return putLease(tx, this.lease.branchId, {
      holder_id: this.lease.holderId,
      epoch: this.lease.epoch,
      expires_at: expiresAt,
    });
  }

  /** The lease is still this one and the stored head is where this writer left it. */
  async #fencedHead(tx: Tx): Promise<Result<BranchRow, LogError>> {
    const live = await this.#checkLease(tx);
    if (!live.ok) return live;
    const branch = await getBranch(tx, this.lease.branchId);
    if (!branch.ok) return branch;
    const expected = this.#chain.fold.seq;
    if (branch.value === undefined || branch.value.head_seq !== expected)
      return err(
        logError("seq_conflict", `the stored head is not ${expected}`),
      );
    return ok(branch.value);
  }

  /** The admitted events, their index rows and their approvals' challenge rows. */
  async #write(
    tx: Tx,
    branch: BranchRow,
    admitted: Admitted,
    now: number,
  ): Promise<Result<void, LogError>> {
    const { events, opened } = admitted;
    await insertEvents(tx, this.lease.branchId, events);
    const own = this.#chain.segments.at(-1)?.header;
    if (own === undefined) throw new Error("a writer's chain has a header");
    const indexed = await indexAppend({
      tx,
      tenant: branch.tenant_id,
      threadId: own.thread_id,
      branchId: own.branch_id,
      events: knownOf(events),
      opened,
      holderId: this.lease.holderId,
      now,
    });
    if (!indexed.ok) return indexed;
    return recordApprovals(tx, branch, events, now);
  }

  async #checkLease(tx: Tx): Promise<Result<void, LogError>> {
    const lease = await getLease(tx, this.lease.branchId);
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
