import {
  ConfigError,
  type Exporter,
  type SkippedBranch,
  type Store,
  type SyncError,
  type SyncReport,
  VERSION,
} from "@threads/core";
import { err, ok, type Result } from "@threads/core/host";
import {
  type ChainEvent,
  type ChangedBranch,
  Feed,
  telemetryBinding,
} from "@threads/core/internal/feed";
import { Backoff } from "./backoff";
import type { Config } from "./env";
import { lossSpans } from "./losses";
import { batches, body, MAX_BATCH } from "./otlp";
import { post } from "./send";
import type { Span } from "./span";
import { type Branch, spans } from "./spans";

// Exporter.sync() (spec/otel/README.md, "The cursor, sync and losses"): every changed branch's
// newly closed spans, posted in batches; a branch's cursor moves only after the collector
// accepted its spans, so a crash re-sends with the same ids and never drops one.

type Ready = {
  readonly branch: ChangedBranch;
  readonly head: number;
  readonly spans: readonly Span[];
};

export class OtelExporter implements Exporter {
  readonly #config: Config;
  readonly #store: Store | undefined;
  readonly #observer: string;
  readonly #content: boolean;
  readonly #backoff: Backoff = new Backoff();
  #queue: Promise<unknown> = Promise.resolve();

  constructor(
    config: Config,
    store: Store | undefined,
    observer: string,
    content: boolean,
  ) {
    this.#config = config;
    this.#store = store;
    this.#observer = observer;
    this.#content = content;
  }

  /** One sync at a time: a second call waits for the first. */
  sync(): Promise<Result<SyncReport, SyncError>> {
    const run = (async (): Promise<Result<SyncReport, SyncError>> => {
      await this.#queue;
      return await this.#sync();
    })();
    this.#queue = settled(run);
    return run;
  }

  async #sync(): Promise<Result<SyncReport, SyncError>> {
    const binding = telemetryBinding(this);
    const signal = binding?.signal;
    const store = this.#store ?? binding?.store;
    if (store === undefined)
      throw new ConfigError(
        "invalid_config",
        "otel(): no store to export: pass otel({store}), or pass the exporter to host({telemetry})",
      );
    const feed = await Feed.open(store, this.#observer);
    await feed.register();
    const changed = await feed.changed();
    if (!changed.ok)
      throw new Error(
        `the store's branch rows are corrupt: ${changed.error.message}`,
      );
    const skipped: SkippedBranch[] = [];
    const ready = await this.#read(feed, changed.value, skipped, signal);
    if (signal?.aborted) return err(stopped());
    await feed.checkpoint(
      ready
        .filter((r) => r.spans.length === 0)
        .map((r) => ({ branch_id: r.branch.branch_id, seq: r.head })),
    );
    const sent = await this.#send(feed, ready, signal);
    if (!sent.ok) return sent;
    const lost = await this.#losses(feed, signal);
    if (!lost.ok) return lost;
    return ok({ spans: sent.value, possiblyLostEvents: lost.value, skipped });
  }

  /**
   * Each changed branch's chain and newly closed spans; one that doesn't read is skipped. It
   * yields to the event loop after each branch, so re-deriving many long threads never holds
   * the host's other work for more than one branch at a time.
   */
  async #read(
    feed: Feed,
    changed: readonly ChangedBranch[],
    skipped: SkippedBranch[],
    signal: AbortSignal | undefined,
  ): Promise<Ready[]> {
    const chains = new Map<string, readonly ChainEvent[] | undefined>();
    const ready: Ready[] = [];
    const now = Date.now();
    for (const branch of changed) {
      await nextTask();
      if (signal?.aborted) break;
      if (this.#backoff.waiting(branch.branch_id, branch.head_seq, now))
        continue;
      const read = await feed.chain(branch.branch_id);
      if (!read.ok) {
        this.#backoff.failed(branch.branch_id, branch.head_seq, now);
        const { branch_id, thread_id, head_seq } = branch;
        skipped.push({ branch_id, thread_id, head_seq, code: read.error.code });
        continue;
      }
      this.#backoff.cleared(branch.branch_id);
      const chain = read.value.events;
      const head = chain.at(-1)?.event.seq ?? branch.head_seq;
      const all = await spansOf(
        feed,
        {
          tenant: branch.tenant_id,
          branchId: branch.branch_id,
          chain,
          content: this.#content,
        },
        chains,
      );
      const fresh = all.filter(
        (s) => s.closeSeq > branch.cursor && s.closeSeq <= head,
      );
      ready.push({ branch, head, spans: fresh });
    }
    return ready;
  }

  /** Posts the spans in batches; after each 2xx, each branch in it moves as far as it is sent. */
  async #send(
    feed: Feed,
    ready: readonly Ready[],
    signal: AbortSignal | undefined,
  ): Promise<Result<number, SyncError>> {
    const byId = new Map<string, Ready>(
      ready.map((r) => [r.branch.branch_id, r]),
    );
    const left = new Map<string, number>(
      ready.map((r) => [r.branch.branch_id, r.spans.length]),
    );
    let sent = 0;
    for (const batch of batches(ready.flatMap((r) => r.spans))) {
      const posted = await post(
        this.#config,
        body(batch, this.#config.resource, VERSION),
        signal,
      );
      if (!posted.ok) return err(posted.error);
      // Stopped after the 2xx: the cursor stays, so these are sent again (never lost).
      if (signal?.aborted) return err(stopped());
      sent += batch.length;
      const through = new Map<string, number>();
      for (const s of batch) {
        left.set(s.branchId, (left.get(s.branchId) ?? 0) - 1);
        through.set(
          s.branchId,
          Math.max(through.get(s.branchId) ?? 0, s.closeSeq),
        );
      }
      await feed.checkpoint(
        [...through].flatMap(([id, seq]) => {
          const r = byId.get(id);
          if (r === undefined) return [];
          return [
            {
              branch_id: r.branch.branch_id,
              seq: left.get(id) === 0 ? r.head : seq,
            },
          ];
        }),
      );
    }
    return ok(sent);
  }

  /** The unreported deletion losses, as possibly_lost spans after the branch batches. */
  async #losses(
    feed: Feed,
    signal: AbortSignal | undefined,
  ): Promise<Result<number, SyncError>> {
    const rows = await feed.unreportedLosses();
    if (!rows.ok)
      throw new Error(
        `the store's loss rows are corrupt: ${rows.error.message}`,
      );
    let lost = 0;
    for (let at = 0; at < rows.value.length; at += MAX_BATCH) {
      const chunk = rows.value.slice(at, at + MAX_BATCH);
      const posted = await post(
        this.#config,
        body(lossSpans(this.#observer, chunk), this.#config.resource, VERSION),
        signal,
      );
      if (!posted.ok) return err(posted.error);
      if (signal?.aborted) return err(stopped());
      await feed.markReported(chunk);
      lost += chunk.reduce((n, r) => n + r.unchecked_events, 0);
    }
    return ok(lost);
  }
}

/**
 * `spans` over chains read from the feed: `spans` is synchronous and the store is not, so it
 * runs until it asks for no chain it doesn't have (each parent it walks names the next one).
 * `chains` caches across branches; an unreadable chain is cached as undefined.
 */
async function spansOf(
  feed: Feed,
  branch: Branch,
  chains: Map<string, readonly ChainEvent[] | undefined>,
): Promise<readonly Span[]> {
  for (;;) {
    const missing = new Set<string>();
    const found = spans(branch, (id) => {
      if (!chains.has(id)) missing.add(id);
      return chains.get(id);
    });
    if (missing.size === 0) return found;
    for (const id of missing) {
      const read = await feed.chain(id);
      chains.set(id, read.ok ? read.value.events : undefined);
    }
  }
}

function stopped(): SyncError {
  return {
    code: "collector_unavailable",
    message:
      "the host stopped: nothing more is sent, and the next start sends it again",
  };
}

/** A macrotask turn: timers, I/O and other work run before the next branch is read. */
function nextTask(): Promise<void> {
  const next = Promise.withResolvers<void>();
  setTimeout(next.resolve, 0);
  return next.promise;
}

/** Waits for `work` to end, whatever its outcome, so the next sync runs after it. */
async function settled(work: Promise<unknown>): Promise<void> {
  try {
    await work;
  } catch {
    // The caller of that sync sees its error.
  }
}
