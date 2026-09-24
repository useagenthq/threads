// A branch that doesn't read is not read again until its back-off passes: 1 s, doubling to 1 h,
// and reset when its head moves (something appended, which may have repaired it).

const FIRST_MS = 1_000;
const MAX_MS = 3_600_000;

type Streak = {
  readonly head: number;
  readonly waitMs: number;
  readonly untilMs: number;
};

export class Backoff {
  readonly #streaks = new Map<string, Streak>();

  /** True while the branch is backed off at the same head. */
  waiting(branchId: string, head: number, now: number): boolean {
    const streak = this.#streaks.get(branchId);
    if (streak === undefined) return false;
    if (streak.head !== head) {
      this.#streaks.delete(branchId);
      return false;
    }
    return now < streak.untilMs;
  }

  failed(branchId: string, head: number, now: number): void {
    const before = this.#streaks.get(branchId);
    const waitMs =
      before === undefined || before.head !== head
        ? FIRST_MS
        : Math.min(before.waitMs * 2, MAX_MS);
    this.#streaks.set(branchId, { head, waitMs, untilMs: now + waitMs });
  }

  cleared(branchId: string): void {
    this.#streaks.delete(branchId);
  }
}
