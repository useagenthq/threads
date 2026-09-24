import type { z } from "zod";
import type { KnownEvent } from "../log";
import { containsSecret, redactStrings } from "../redact";
import { err, ok, type Result } from "../result";
import {
  addLine,
  type Chain,
  type ChainEvent,
  type VerifiedLog,
} from "../verify";
import { type LogError, logError } from "../verify/error";
import { VERSION } from "../version";
import { recordApprovals } from "./approvals";
import type { SqliteDriver } from "./driver";
import { canonicalLine, uuidv7 } from "./encode";
import {
  atomically,
  getBranch,
  getLease,
  insertEvents,
  putLease,
} from "./tables";
import { recordWakes } from "./wakes";

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
/**
 * An event before the writer fills its envelope. It is parsed like any stored line. A draft may
 * bring its own `event_id` (minted with `uuidv7` from the writer's clock) when a later event of
 * the same append must name it; the line schema checks its format and validate_next that it is
 * unique.
 */
export type EventDraft = DistributiveOmit<
  z.input<typeof KnownEvent>,
  EnvelopeKey
> & { readonly event_id?: string };

export type Lease = {
  readonly branchId: string;
  readonly holderId: string;
  readonly epoch: number;
  /** How long the lease lasts from each take or renewal. */
  readonly ttlMs: number;
};

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

  /** The committed chain this writer appends to. Each append replaces it; none mutates it. */
  get chain(): Chain {
    return this.#chain;
  }

  /**
   * Appends drafts in order and returns once their transaction has committed with full sync.
   * `alongside` writes host rows (an idempotency receipt, a consumed inbox item) in the same
   * transaction, after the events; an error from it rolls the append back.
   */
  append(
    drafts: readonly EventDraft[],
    alongside?: (added: readonly ChainEvent[]) => Result<void, LogError>,
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
      this.#commit(expectedSeq, added, alongside),
    );
    if (!committed.ok) {
      const { code } = committed.error;
      this.#poisoned = code === "stale_epoch" || code === "seq_conflict";
      return committed;
    }
    this.#chain = trial;
    return ok(added);
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

  #admit(trial: Chain, draft: EventDraft): Result<ChainEvent, LogError> {
    const segment = trial.segments.at(-1);
    if (segment === undefined) throw new Error("a writer's chain has a header");
    const now = this.#now();
    // Nothing is recorded with a resolved secret in it (C5): every event passes here.
    const actor = redactStrings(draft.actor);
    const data = redactStrings(draft.data);
    const content = canonicalLine({ actor, data });
    if (!content.ok) return content;
    // Canonical escaping or JSON punctuation can still join redacted strings into a value.
    if (containsSecret(content.value))
      return err(
        logError(
          "secret_in_stored_bytes",
          "the event's stored bytes would hold a registered secret; nothing appended",
        ),
      );
    const line = canonicalLine({
      ...draft,
      actor,
      data,
      seq: trial.fold.seq + 1,
      event_id: draft.event_id ?? uuidv7(now),
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
    alongside?: (added: readonly ChainEvent[]) => Result<void, LogError>,
  ): Result<void, LogError> {
    const live = this.#checkLease();
    if (!live.ok) return live;
    const branch = getBranch(this.#db, this.lease.branchId);
    if (!branch.ok) return branch;
    if (branch.value === undefined || branch.value.head_seq !== expectedSeq)
      return err(
        logError("seq_conflict", `the stored head is not ${expectedSeq}`),
      );
    insertEvents(this.#db, this.lease.branchId, added);
    recordWakes(this.#db, this.lease.branchId, added);
    const approvals = recordApprovals(
      this.#db,
      branch.value,
      added,
      this.#now(),
    );
    if (!approvals.ok || alongside === undefined) return approvals;
    return alongside(added);
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
