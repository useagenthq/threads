import type { z } from "zod";
import type { KnownEvent } from "../log";
import { err, ok, type Result } from "../result";
import { addLine, type Chain, type ChainEvent } from "../verify";
import { type LogError, logError } from "../verify/error";
import type { SqliteDriver } from "./driver";
import { canonicalLine, uuidv7 } from "./encode";
import {
  atomically,
  getBranch,
  getLease,
  insertEvents,
  putLease,
} from "./tables";

type EnvelopeKey =
  | "seq"
  | "event_id"
  | "thread_id"
  | "branch_id"
  | "epoch"
  | "time"
  | "prev_hash";
type DistributiveOmit<T, K extends PropertyKey> = T extends unknown
  ? Omit<T, K>
  : never;
/** An event before the writer fills its envelope. It is parsed like any stored line. */
export type EventDraft = DistributiveOmit<
  z.input<typeof KnownEvent>,
  EnvelopeKey
>;

export type Lease = {
  readonly branchId: string;
  readonly holderId: string;
  readonly epoch: number;
};

/**
 * The single writer of one branch under one lease epoch. Every append runs the
 * same admission as import, then commits only if the lease is still this one and the stored
 * head is where this writer left it. A stale lease or a moved head poisons the writer.
 */
export class Writer {
  readonly #db: SqliteDriver;
  readonly #now: () => number;
  readonly lease: Lease;
  #chain: Chain;
  #poisoned = false;

  constructor(db: SqliteDriver, now: () => number, lease: Lease, chain: Chain) {
    this.#db = db;
    this.#now = now;
    this.lease = lease;
    this.#chain = chain;
  }

  /** Appends drafts in order and returns once their transaction has committed with full sync. */
  append(
    drafts: readonly EventDraft[],
  ): Result<readonly ChainEvent[], LogError> {
    if (this.#poisoned)
      return err(
        logError("writer_poisoned", "this writer lost its lease or head"),
      );
    const trial = copy(this.#chain);
    const added: ChainEvent[] = [];
    for (const draft of drafts) {
      const event = this.#admit(trial, draft);
      if (!event.ok) return event;
      added.push(event.value);
    }
    const expectedSeq = this.#chain.fold.seq;
    const committed = atomically(this.#db, () =>
      this.#commit(expectedSeq, added),
    );
    if (!committed.ok) {
      const { code } = committed.error;
      this.#poisoned = code === "stale_epoch" || code === "seq_conflict";
      return committed;
    }
    this.#chain = trial;
    return ok(added);
  }

  /** Extends the lease; fails (and poisons) if another holder or epoch took it. */
  renew(ttlMs: number): Result<void, LogError> {
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

  #admit(trial: Chain, draft: EventDraft): Result<ChainEvent, LogError> {
    const segment = trial.segments.at(-1);
    if (segment === undefined) throw new Error("a writer's chain has a header");
    const now = this.#now();
    const line = canonicalLine({
      ...draft,
      seq: trial.fold.seq + 1,
      event_id: uuidv7(now),
      thread_id: segment.header.thread_id,
      branch_id: segment.header.branch_id,
      epoch: this.lease.epoch,
      time: now,
      prev_hash: segment.events.at(-1)?.hash ?? segment.hash,
    });
    if (!line.ok) return line;
    const admitted = addLine(trial, line.value);
    if (!admitted.ok) return admitted;
    const event = trial.events.at(-1);
    if (event === undefined)
      throw new Error("an admitted event is on the chain");
    return ok(event);
  }

  #commit(
    expectedSeq: number,
    added: readonly ChainEvent[],
  ): Result<void, LogError> {
    const live = this.#checkLease();
    if (!live.ok) return live;
    const branch = getBranch(this.#db, this.lease.branchId);
    if (!branch.ok) return branch;
    if (branch.value?.head_seq !== expectedSeq)
      return err(
        logError("seq_conflict", `the stored head is not ${expectedSeq}`),
      );
    insertEvents(this.#db, this.lease.branchId, added);
    return ok(undefined);
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

/** A trial copy of a chain: new arrays and fold, shared immutable events. */
// ponytail: O(chain length) per append batch; keep an undo log instead if batches get hot.
function copy(chain: Chain): Chain {
  return {
    segments: chain.segments.map((s) => ({ ...s, events: [...s.events] })),
    events: [...chain.events],
    fold: structuredClone(chain.fold),
  };
}
