import type { LiveHub } from "./hub";
import type { Delta } from "./live";

// One UI connection's end of the live hub: the deltas it has not handled yet, and a wait that
// ends at the next delta or append of this process, or after a poll interval for everything
// another process appends. A connection without live text (the cursor route) only polls.

const POLL_MS = 50;

export class LiveListener {
  /** Parts a delta had already gone out for when this connection registered. */
  readonly missed: ReadonlySet<string> | undefined;
  readonly #inbox: Delta[] = [];
  #wake: (() => void) | undefined;
  readonly #stop: () => void;

  /** Registers now: call it before reading the log's head, and before starting the run. */
  constructor(hub: LiveHub | undefined, thread: string) {
    if (hub === undefined) {
      this.missed = undefined;
      this.#stop = () => undefined;
      return;
    }
    const registered = hub.listen(thread, (item) => {
      if (item.kind === "delta") this.#inbox.push(item.delta);
      this.#wake?.();
    });
    this.missed = registered.missed;
    this.#stop = registered.stop;
  }

  /** The deltas received since the last call, in order. */
  take(): readonly Delta[] {
    return this.#inbox.splice(0);
  }

  /** Until something may have changed. */
  async next(): Promise<void> {
    if (this.#inbox.length > 0) return;
    const { promise, resolve } = Promise.withResolvers<void>();
    this.#wake = resolve;
    const timer = setTimeout(resolve, POLL_MS);
    await promise;
    clearTimeout(timer);
    this.#wake = undefined;
  }

  stop(): void {
    this.#stop();
  }
}
