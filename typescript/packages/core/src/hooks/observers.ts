import type { BranchId, KnownEvent } from "../log";
import type { ObserverCursors } from "../store/cursors";

// Observers: committed events delivered after
// append, asynchronously, in log order. They can lag and fail without touching execution or the
// log: each keeps a durable cursor, advanced only after its handler resolved, so a failed or
// interrupted delivery is retried from the cursor on the next poke or the next run.

export type ObserverHandler = (event: KnownEvent) => Promise<void>;

export type Observer = {
  readonly name: string;
  /** Handlers keyed by event type, or "*" for every event. */
  readonly on: Readonly<Record<string, ObserverHandler>>;
};

type Lane = { running: Promise<void> | undefined; again: boolean };

export class ObserverPump {
  readonly #cursors: ObserverCursors;
  readonly #branch: BranchId;
  readonly #events: () => readonly KnownEvent[];
  readonly #lanes: ReadonlyMap<Observer, Lane>;

  constructor(
    cursors: ObserverCursors,
    branch: BranchId,
    events: () => readonly KnownEvent[],
    observers: readonly Observer[],
  ) {
    this.#cursors = cursors;
    this.#branch = branch;
    this.#events = events;
    this.#lanes = new Map(
      observers.map((o) => [o, { running: undefined, again: false }]),
    );
  }

  /** New events may be committed: every idle observer starts catching up. Never blocks. */
  poke(): void {
    for (const [observer, lane] of this.#lanes) {
      if (lane.running !== undefined) {
        lane.again = true;
        continue;
      }
      lane.running = this.#drain(observer, lane);
    }
  }

  /** Resolves once every observer that isn't stuck in a handler has caught up (tests, shutdown). */
  async idle(): Promise<void> {
    await Promise.all(
      [...this.#lanes.values()].map((lane) => lane.running ?? undefined),
    );
  }

  async #drain(observer: Observer, lane: Lane): Promise<void> {
    try {
      do {
        lane.again = false;
        await this.#deliver(observer);
      } while (lane.again);
    } catch {
      // A failed handler leaves its cursor where it was; the next poke retries from there.
    } finally {
      lane.running = undefined;
    }
  }

  async #deliver(observer: Observer): Promise<void> {
    const cursor = this.#cursors.get(observer.name, this.#branch);
    if (!cursor.ok) return;
    for (const event of this.#events().filter((e) => e.seq > cursor.value)) {
      const handler = observer.on[event.type] ?? observer.on["*"];
      // A copy: an observer can't change the event the loop holds.
      if (handler !== undefined) await handler(structuredClone(event));
      this.#cursors.advance(observer.name, this.#branch, event.seq);
    }
  }
}
