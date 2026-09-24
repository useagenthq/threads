import type { Fold } from "../fold/state";
import type { ArtifactRef, BranchId, KnownEvent, ThreadId } from "../log";
import type { ModelContext } from "../model";
import { contextReader } from "../model/context";
import {
  containsSecret,
  redactSecrets,
  SecretInProviderOutput,
} from "../redact";
import { knownEvents, type ReducedState, reduce } from "../reduce";
import { err, ok, type Result } from "../result";
import type { ArtifactStore, EventDraft, Writer } from "../store";
import { type DecideTx, isRefusal, type Refusal } from "../store/writer";
import { Batch } from "../team/batch";
import { turnProvenance } from "../team/provenance";
import { settle } from "../team/settle";
import { settlementOf } from "../team/turn-end";
import type { Chain, ChainEvent } from "../verify";
import type { LogError } from "../verify/error";
import { afterBarrier, opensWork } from "./turn";
import type { ChildEnd, Halt, LoopConfig, TeamRuntime } from "./types";
import { BARRED, type Barred } from "./types";

const encoder = new TextEncoder();

/**
 * One run's view of its branch: the writer that holds the lease, the artifacts, and the loop
 * config. All state is read back from the committed chain (invariant 1).
 */
export class Session {
  readonly #writer: Writer;
  readonly artifacts: ArtifactStore;
  readonly config: LoopConfig;
  /** Background children running in this process, by spawn call id. */
  readonly background: Map<string, Promise<void>> = new Map();
  /** Ended background children the loop records at its next step boundary. */
  readonly finished: Map<string, ChildEnd | Halt> = new Map();
  #cache: { readonly chain: Chain; readonly events: readonly KnownEvent[] };

  constructor(writer: Writer, artifacts: ArtifactStore, config: LoopConfig) {
    this.#writer = writer;
    this.artifacts = artifacts;
    this.config = config;
    this.#cache = { chain: writer.chain, events: knownEvents(writer.chain) };
  }

  /** The resolved chain's known events. */
  get events(): readonly KnownEvent[] {
    const chain = this.#writer.chain;
    if (this.#cache.chain !== chain)
      this.#cache = { chain, events: knownEvents(chain) };
    return this.#cache.events;
  }

  /** Settles on the branch's next committed append, a control's included. */
  moved(): Promise<void> {
    return this.#writer.moved();
  }

  get fold(): Fold {
    return this.#writer.chain.fold;
  }

  get branchId(): BranchId {
    const header = this.#writer.chain.segments.at(-1)?.header;
    if (header === undefined) throw new Error("a writer's chain has a header");
    return header.branch_id;
  }

  get threadId(): ThreadId {
    const header = this.#writer.chain.segments[0]?.header;
    if (header === undefined) throw new Error("a writer's chain has a header");
    return header.thread_id;
  }

  now(): number {
    return this.config.clock.now();
  }

  /** reduce(log) now: what state-taking hooks see. */
  state(): ReducedState {
    return reduce(this.#writer.chain, this.now());
  }

  /**
   * Appends in one fenced transaction. A lost lease or a moved head is a halt: this writer must
   * never append or dispatch again.
   */
  append(...drafts: readonly EventDraft[]): Halt | undefined {
    if (drafts.some(opensWork))
      throw new Error("a batch that starts work is appended with appendWork");
    return this.#admit(drafts);
  }

  /**
   * Appends a batch that starts new work (a request, a dispatch, a handoff, a child, a retry
   * wait, a model switch). BARRED: a pending cancel refused it; its hook decisions were kept, and
   * the cancellation step is next.
   */
  appendWork(...drafts: readonly EventDraft[]): Halt | Barred | undefined {
    const refused =
      afterBarrier(this.fold, this.events, drafts).length !== drafts.length;
    const stopped = this.#admit(drafts);
    return stopped ?? (refused ? BARRED : undefined);
  }

  /**
   * A decided append (a team operation): `decide` reads the store inside the append's
   * transaction and builds the batch, which passes the cancel barrier as `appendWork`'s does, or
   * refuses. A refusal appends nothing and comes back for the caller to record.
   */
  appendDecided<E>(
    decide: (tx: DecideTx) => Result<readonly EventDraft[], E>,
  ): Halt | Barred | Refusal<E> | undefined {
    const batch = { barred: false };
    const appended = this.#writer.appendDecided((tx) => {
      const decided = decide(tx);
      if (!decided.ok) return decided;
      const kept = afterBarrier(this.fold, this.events, decided.value);
      batch.barred = kept.length !== decided.value.length;
      return ok(kept);
    });
    if (isRefusal(appended)) return appended;
    return this.#committed(appended) ?? (batch.barred ? BARRED : undefined);
  }

  #admit(drafts: readonly EventDraft[]): Halt | undefined {
    const admitted = afterBarrier(this.fold, this.events, drafts);
    if (admitted.length === 0) return undefined;
    const team = this.config.team;
    if (team !== undefined && admitted.some((d) => d.type === "turn_completed"))
      return this.#settled(team, admitted);
    return this.#committed(this.#writer.append(admitted));
  }

  /**
   * A team thread's turn end carries its settlement in the same append (spec/schema/README.md,
   * "Teams", Settling): member_idle or member_ended with the notifications, refusals and, for a
   * lead, the cancels they send, decided from the rows in the append's transaction.
   */
  #settled(team: TeamRuntime, drafts: readonly EventDraft[]): Halt | undefined {
    const events = this.events;
    const turn = events.slice(
      events.findLastIndex((e) => e.type === "turn_completed") + 1,
    );
    const how = settlementOf(turn, drafts);
    const appended = this.#writer.appendDecided((tx) => {
      const batch = new Batch(tx.chain.fold.seq, tx.now, team.mint);
      for (const d of drafts) batch.add(d);
      const provenance = turnProvenance(tx.db, tx.chain);
      if (how !== undefined && provenance !== undefined)
        settle(
          {
            db: tx.db,
            batch,
            threadId: this.threadId,
            branchId: this.branchId,
            provenance,
            put: (text) => this.store(text, "text/plain"),
          },
          how,
        );
      return ok(batch.drafts);
    });
    if (isRefusal(appended)) throw new Error("a settlement never refuses");
    return this.#committed(appended);
  }

  /** A lost lease or head halts the run; committed events go to `onEvent`. */
  #committed(
    appended: Result<readonly ChainEvent[], LogError>,
  ): Halt | undefined {
    if (!appended.ok) {
      const { code, message } = appended.error;
      return code === "secret_in_stored_bytes"
        ? { code, message }
        : { code: "branch_busy", message };
    }
    const events = this.events;
    for (const e of events.slice(events.length - appended.value.length))
      this.config.onEvent?.(e);
    this.config.team?.notify();
    return undefined;
  }

  /**
   * Called right before every adapter call (model send and lookup, tool run, lookup and
   * terminate): a writer that lost its lease or epoch never reaches the adapter.
   */
  fence(): Halt | undefined {
    const live = this.#writer.fence();
    return live.ok
      ? undefined
      : { code: "branch_busy", message: live.error.message };
  }

  /** The lease epoch every dispatch carries. */
  get epoch(): number {
    return this.#writer.lease.epoch;
  }

  /**
   * The context a model send or lookup carries, bound to this writer: its fence, reads and
   * stores act for this branch and epoch only (spec/api.json ModelContext).
   */
  modelContext(): ModelContext {
    return {
      branchId: this.branchId,
      epoch: this.epoch,
      fence: async () => {
        const halted = this.fence();
        return halted === undefined
          ? ok(undefined)
          : err({ code: "stale_epoch", message: halted.message });
      },
      read: contextReader(this.artifacts),
      put: async (data, mediaType) => {
        // Replayed byte-exact, so never edited: a secret in it ends the turn instead.
        if (containsSecret(data)) throw new SecretInProviderOutput();
        return this.store(data, mediaType);
      },
    };
  }

  /** Stores bytes before any event names them, and returns their ref. Text is redacted (C5). */
  store(bytes: Uint8Array | string, mediaType: string): ArtifactRef {
    const data =
      typeof bytes === "string" ? encoder.encode(redactSecrets(bytes)) : bytes;
    return {
      sha256: this.artifacts.put(data),
      bytes: data.length,
      media_type: mediaType,
    };
  }
}
