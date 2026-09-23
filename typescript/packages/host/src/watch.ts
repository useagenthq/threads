import type { ThreadId } from "@threads/core/host";

// The channel threads a host looks at on every tick until it sees each one settled: every one a
// crash may have left mid-run or owing replies, and every one this host consumed an item on.

export type Watched = { readonly tenant: string; readonly id: ThreadId };

export class Watch {
  readonly #threads = new Map<string, Watched>();

  /** Watches the thread again, even when it is watched already. */
  add(tenant: string, id: ThreadId): void {
    this.#threads.set(id, { tenant, id });
  }

  has(id: ThreadId): boolean {
    return this.#threads.has(id);
  }

  /**
   * Each watched thread, with how to drop it once a pass saw it settled. A thread watched again
   * while that pass ran stays watched: the verdict is about what the pass saw, not what came since.
   */
  entries(): readonly {
    readonly thread: Watched;
    readonly settled: () => void;
  }[] {
    return [...this.#threads.values()].map((thread) => ({
      thread,
      settled: () => {
        if (this.#threads.get(thread.id) === thread)
          this.#threads.delete(thread.id);
      },
    }));
  }
}
