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
  boundStore,
  type ChainEvent,
  type ChangedBranch,
  Feed,
} from "@threads/core/internal/feed";
import { Backoff } from "./backoff";
import type { Config } from "./env";
import { lossSpans } from "./losses";
import { batches, body, MAX_BATCH } from "./otlp";
import { post } from "./send";
import type { Span } from "./span";
import { spans } from "./spans";

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
    const run = this.#queue.then(() => this.#sync());
    this.#queue = run.catch(() => undefined);
    return run;
  }

  async #sync(): Promise<Result<SyncReport, SyncError>> {
    const store = this.#store ?? boundStore(this);
    if (store === undefined)
      throw new ConfigError(
        "invalid_config",
        "otel(): no store to export: pass otel({store}), or pass the exporter to host({telemetry})",
      );
    const feed = await Feed.open(store, this.#observer);
    feed.register();
    const changed = feed.changed();
    if (!changed.ok)
      throw new Error(
        `the store's branch rows are corrupt: ${changed.error.message}`,
      );
    const skipped: SkippedBranch[] = [];
    const ready = this.#read(feed, changed.value, skipped);
    feed.checkpoint(
      ready
        .filter((r) => r.spans.length === 0)
        .map((r) => ({ branch_id: r.branch.branch_id, seq: r.head })),
    );
    const sent = await this.#send(feed, ready);
    if (!sent.ok) return sent;
    const lost = await this.#losses(feed);
    if (!lost.ok) return lost;
    return ok({ spans: sent.value, possiblyLostEvents: lost.value, skipped });
  }

  /** Each changed branch's chain and newly closed spans; one that doesn't read is skipped. */
  #read(
    feed: Feed,
    changed: readonly ChangedBranch[],
    skipped: SkippedBranch[],
  ): Ready[] {
    const chains = new Map<string, readonly ChainEvent[] | undefined>();
    const lookup = (id: string): readonly ChainEvent[] | undefined => {
      if (!chains.has(id)) {
        const read = feed.chain(id);
        chains.set(id, read.ok ? read.value.events : undefined);
      }
      return chains.get(id);
    };
    const ready: Ready[] = [];
    const now = Date.now();
    for (const branch of changed) {
      if (this.#backoff.waiting(branch.branch_id, branch.head_seq, now))
        continue;
      const read = feed.chain(branch.branch_id);
      if (!read.ok) {
        this.#backoff.failed(branch.branch_id, branch.head_seq, now);
        const { branch_id, thread_id, head_seq } = branch;
        skipped.push({ branch_id, thread_id, head_seq, code: read.error.code });
        continue;
      }
      this.#backoff.cleared(branch.branch_id);
      const chain = read.value.events;
      const head = chain.at(-1)?.event.seq ?? branch.head_seq;
      const all = spans(
        {
          tenant: branch.tenant_id,
          branchId: branch.branch_id,
          chain,
          content: this.#content,
        },
        lookup,
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
      );
      if (!posted.ok) return err(posted.error);
      sent += batch.length;
      const through = new Map<string, number>();
      for (const s of batch) {
        left.set(s.branchId, (left.get(s.branchId) ?? 0) - 1);
        through.set(
          s.branchId,
          Math.max(through.get(s.branchId) ?? 0, s.closeSeq),
        );
      }
      feed.checkpoint(
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
  async #losses(feed: Feed): Promise<Result<number, SyncError>> {
    const rows = feed.unreportedLosses();
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
      );
      if (!posted.ok) return err(posted.error);
      feed.markReported(chunk);
      lost += chunk.reduce((n, r) => n + r.unchecked_events, 0);
    }
    return ok(lost);
  }
}
