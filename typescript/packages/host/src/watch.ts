import type { BranchId, ThreadId } from "@threads/core/host";

// The threads a host looks at on every tick until it sees each one settled: every channel thread
// and API run branch a crash may have left mid-run or owing replies, and every channel thread
// this host consumed an item on.

/** A thread at `branch`; without one, its main branch (a channel conversation). */
export type Watched = {
  readonly tenant: string;
  readonly id: ThreadId;
  readonly branch?: BranchId;
};

export class Watch {
  readonly #threads = new Map<string, Watched>();

  /** Watches the thread (at `branch`) again, even when it is watched already. */
  add(tenant: string, id: ThreadId, branch?: BranchId): void {
    this.#threads.set(
      branch ?? id,
      branch === undefined ? { tenant, id } : { tenant, id, branch },
    );
  }

  has(key: ThreadId | BranchId): boolean {
    return this.#threads.has(key);
  }

  /**
   * Each watched thread, with how to drop it once a pass saw it settled. A thread watched again
   * while that pass ran stays watched: the verdict is about what the pass saw, not what came since.
   */
  entries(): readonly {
    readonly thread: Watched;
    readonly settled: () => void;
  }[] {
    return [...this.#threads.entries()].map(([key, thread]) => ({
      thread,
      settled: () => {
        if (this.#threads.get(key) === thread) this.#threads.delete(key);
      },
    }));
  }
}
