import type { Exporter, SkippedBranch, Store } from "@threads/core";
import { bindTelemetry } from "@threads/core/host";

// host({telemetry}): the exporter syncs on its own timer, beside the tick and never inside it, so
// a slow or dead collector holds up no run, schedule or reply. A failing collector is retried
// with back-off (1 s doubling to 60 s) and logged once per streak; a branch the exporter can't
// read is logged once per head it fails at (its back-off streak).

/** How often it syncs, how long a failing collector's back-off grows, and the bound on stop(). */
export type Timing = {
  readonly everyMs: number;
  readonly maxWaitMs: number;
  readonly lastSyncMs: number;
};

const TIMING: Timing = { everyMs: 1_000, maxWaitMs: 60_000, lastSyncMs: 5_000 };

export class Telemetry {
  readonly #exporter: Exporter;
  readonly #logged = new Set<string>();
  #timer: ReturnType<typeof setInterval> | undefined;
  #running: Promise<void> | undefined;
  #waitMs = 0;
  #nextAt = 0;

  readonly #timing: Timing;
  /** Aborted by stop(): the exporter's POST in flight ends, and no cursor moves after it. */
  readonly #stopped = new AbortController();

  constructor(exporter: Exporter, store: Store, timing: Timing = TIMING) {
    this.#exporter = exporter;
    this.#timing = timing;
    bindTelemetry(exporter, { store, signal: this.#stopped.signal });
  }

  start(): void {
    this.#timer ??= setInterval(() => {
      if (this.#running === undefined && Date.now() >= this.#nextAt)
        this.#running = this.#sync().finally(() => {
          this.#running = undefined;
        });
    }, this.#timing.everyMs);
  }

  /**
   * stop(): no new tick, then one last sync once the one in flight is done, bounded by 5 s. Then
   * the exporter is aborted and awaited, so nothing of it runs after stop() returns (an exporter
   * that ignores the abort is given one more bound, then left).
   */
  async stop(): Promise<void> {
    clearInterval(this.#timer);
    this.#timer = undefined;
    const last = (async (): Promise<void> => {
      await this.#running;
      await this.#sync();
    })();
    await this.#within(last);
    this.#stopped.abort();
    await this.#within(last);
  }

  async #within(work: Promise<void>): Promise<void> {
    const bound = Promise.withResolvers<void>();
    const timer = setTimeout(bound.resolve, this.#timing.lastSyncMs);
    await Promise.race([work, bound.promise]);
    clearTimeout(timer);
  }

  async #sync(): Promise<void> {
    try {
      const sent = await this.#exporter.sync();
      if (sent.ok) {
        this.#waitMs = 0;
        this.#nextAt = 0;
        for (const s of sent.value.skipped) this.#skipped(s);
        return;
      }
      if (this.#waitMs === 0)
        console.error(
          `threads host: telemetry ${sent.error.code}: ${sent.error.message}; retrying with backoff`,
        );
      const { everyMs, maxWaitMs } = this.#timing;
      this.#waitMs = Math.min(Math.max(this.#waitMs * 2, everyMs), maxWaitMs);
      this.#nextAt = Date.now() + this.#waitMs;
    } catch (error) {
      console.error("threads host: telemetry failed", error);
    }
  }

  #skipped(s: SkippedBranch): void {
    const key = `${s.branch_id}@${s.head_seq}`;
    if (this.#logged.has(key)) return;
    this.#logged.add(key);
    console.error(
      `threads host: telemetry skipped branch ${s.branch_id} of thread ${s.thread_id} (${s.code}); retrying with backoff`,
    );
  }
}
