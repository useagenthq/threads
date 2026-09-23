import type { Fold } from "../fold/state";
import type { ArtifactRef, BranchId, KnownEvent } from "../log";
import { knownEvents } from "../reduce";
import type { ArtifactStore, EventDraft, Writer } from "../store";
import type { Chain } from "../verify";
import type { Halt, LoopConfig } from "./types";

const encoder = new TextEncoder();

/**
 * One run's view of its branch: the writer that holds the lease, the artifacts, and the loop
 * config. All state is read back from the committed chain (invariant 1).
 */
export class Session {
  readonly #writer: Writer;
  readonly artifacts: ArtifactStore;
  readonly config: LoopConfig;
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

  get fold(): Fold {
    return this.#writer.chain.fold;
  }

  get branchId(): BranchId {
    const header = this.#writer.chain.segments.at(-1)?.header;
    if (header === undefined) throw new Error("a writer's chain has a header");
    return header.branch_id;
  }

  now(): number {
    return this.config.clock.now();
  }

  /**
   * Appends in one fenced transaction. A lost lease or a moved head is a halt: this writer must
   * never append or dispatch again.
   */
  append(...drafts: readonly EventDraft[]): Halt | undefined {
    const appended = this.#writer.append(drafts);
    if (!appended.ok)
      return { code: "branch_busy", message: appended.error.message };
    const events = this.events;
    for (const e of events.slice(events.length - appended.value.length))
      this.config.onEvent?.(e);
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

  /** Stores bytes before any event names them, and returns their ref. */
  store(bytes: Uint8Array | string, mediaType: string): ArtifactRef {
    const data = typeof bytes === "string" ? encoder.encode(bytes) : bytes;
    return {
      sha256: this.artifacts.put(data),
      bytes: data.length,
      media_type: mediaType,
    };
  }
}
