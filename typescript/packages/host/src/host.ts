import { type Agent, type ChannelAdapter, ConfigError } from "@threads/core";
import {
  type EventId,
  type Principal,
  type Result,
  type Sandbox,
  type Store,
  storeConnection,
  ThreadId,
} from "@threads/core/host";
import { consume } from "./consume";
import { type HostCeiling, HostContext } from "./context";
import { failure } from "./errors";
import { type Authenticate, api } from "./http";
import { channelThreads } from "./inbox";
import { challenge, receive } from "./intake";
import { unfinishedRuns } from "./receipts";
import { Recovery } from "./recovery";
import { type RunAccepted, type StartRunCode, startRun } from "./runs";
import { bindSchedules, type Schedule, tick } from "./schedules";
import type { StartRunRequest } from "./schemas";
import { type SseMessage, subscribe } from "./subscribe";
import { Watch } from "./watch";

// host() (spec/api.json): binds agents to a store, channels
// and schedules. It starts nothing until ready(), which confirms the bindings, sends nothing and
// starts no run; after it, the host consumes durable intake and fires due schedules. stop()
// first aborts: runs get their abort signal, and no send begins. A send whose request never
// reached the transport fence is abandoned (it stays potentially sent, for the next host to
// reconcile); one that passed the fence keeps its lease until it settles. Then stop() waits for
// the work in flight to return and releases leases. It has no deadline of its own: a tool that
// ignores its signal is waited on, since returning while it can still act would break the
// fence. Deadlines belong at the tool or provider boundary.

export type HostOptions = {
  readonly store: Store;
  /** Keyed by the name channels, schedules and the HTTP API use. */
  readonly agents: Readonly<Record<string, Agent<never, unknown>>>;
  readonly channels?: Readonly<Record<string, ChannelAdapter>>;
  readonly schedules?: readonly Schedule[];
  /** Maps an HTTP API request to a principal, or null for 401. Absent: every /v1 route is 401. */
  readonly authenticate?: Authenticate;
  /** The host ceiling every run of this host is also decided under. */
  readonly ceiling?: HostCeiling;
};

export type Host = {
  /** Mount in Next, Hono, Bun.serve. */
  readonly fetch: (request: Request) => Promise<Response>;
  /** The channel keys; each one's webhook is POST /channels/<key>/events. */
  readonly channels: readonly string[];
  readonly startRun: (
    request: StartRunRequest,
    options: { readonly principal: Principal; readonly idempotencyKey: string },
  ) => Promise<
    Result<
      RunAccepted,
      { readonly code: StartRunCode; readonly message: string }
    >
  >;
  readonly subscribe: (
    threadId: ThreadId,
    runId: EventId,
    options: { readonly principal: Principal; readonly afterSeq?: number },
  ) => Promise<
    Result<
      AsyncIterable<SseMessage>,
      { readonly code: "forbidden" | "not_found"; readonly message: string }
    >
  >;
  readonly ready: () => Promise<void>;
  readonly stop: () => Promise<void>;
  readonly [Symbol.asyncDispose]: () => Promise<void>;
};

const TICK_MS = 1_000;

/** The sandbox providers of a host's agents, for `threads gc` to release their resources. */
const SANDBOXES = new WeakMap<Host, readonly Sandbox[]>();

export function hostSandboxes(h: Host): readonly Sandbox[] {
  return SANDBOXES.get(h) ?? [];
}

/** Each host's wait for its next complete tick. */
const TICKS = new WeakMap<Host, () => Promise<void>>();

/**
 * Resolves after the next tick that starts after the call has finished: lets a test assert
 * what a tick did (or didn't) without sleeping. Internal: not exported from the package.
 */
export function hostTicked(h: Host): Promise<void> {
  const next = TICKS.get(h);
  if (next === undefined) throw new Error("not a host");
  return next();
}

export function host(options: HostOptions): Host {
  const ctx = new HostContext(
    options.store,
    options.agents,
    options.channels ?? {},
    options.ceiling,
  );
  const consuming = new Map<string, Promise<void>>();
  let timer: ReturnType<typeof setInterval> | undefined;
  let ticking: Promise<void> | undefined;
  let tickWaiters: (() => void)[] = [];
  const watch = new Watch();
  const recovery = new Recovery(ctx);
  // A run of this host that meets a store outage is run on by recovery, with backoff.
  ctx.onStoreOutage = (tenant, thread, error) => {
    recovery.failed(thread.branch, error);
    watch.add(tenant, thread.id, thread.branch);
  };
  let seeded = false;

  /** One consumer per thread in this process; a kick while one runs is picked up by it. */
  const kick = (tenant: string, threadId: string): void => {
    if (consuming.has(threadId)) return;
    const id = ThreadId.parse(threadId);
    const running = (async (): Promise<void> => {
      try {
        await consume(ctx, tenant, id);
      } catch (error) {
        console.error(`threads host: consuming ${threadId} failed`, error);
      } finally {
        consuming.delete(threadId);
        // Watched until settled: another host's short lease can take the branch between an
        // input's append and its run's own lease, and then no process runs the open turn.
        watch.add(tenant, id);
      }
    })();
    consuming.set(threadId, running);
  };

  const kickAll = (
    threads: readonly { readonly tenant: string; readonly threadId: string }[],
  ): void => {
    for (const t of threads) kick(t.tenant, t.threadId);
  };

  const sweep = async (): Promise<void> => {
    const { db } = await storeConnection(ctx.store);
    const rows = db.all(
      "SELECT DISTINCT tenant_id, thread_id FROM inbox WHERE consumed_seq IS NULL",
      [],
    );
    for (const row of rows)
      if (
        typeof row === "object" &&
        row !== null &&
        "tenant_id" in row &&
        "thread_id" in row &&
        typeof row.tenant_id === "string" &&
        typeof row.thread_id === "string"
      )
        kick(row.tenant_id, row.thread_id);
  };

  /** From the first tick after ready(), never inside it: ready() sends nothing. */
  const recoverReplies = async (): Promise<void> => {
    if (!seeded) {
      const { db } = await storeConnection(ctx.store);
      for (const r of channelThreads(db)) watch.add(r.tenant_id, r.thread_id);
      // ponytail: API runs are found at start only; a live peer's crash waits for a restart.
      for (const r of unfinishedRuns(db))
        watch.add(r.tenant_id, r.thread_id, r.branch_id);
      seeded = true;
    }
    // Side by side: one thread's slow reply never holds up another's recovery.
    await Promise.all(
      watch.entries().map(async ({ thread: t, settled }) => {
        if ((await recovery.look(t.tenant, t.id, t.branch)) === "done")
          settled();
      }),
    );
  };

  const stop = async (): Promise<void> => {
    clearInterval(timer);
    timer = undefined;
    // Runs are aborted first: a tick or a consumer may be waiting on one.
    ctx.abort();
    await ticking;
    await Promise.all(consuming.values());
    await ctx.stop();
  };

  const made: Host = {
    channels: [...ctx.channels.keys()],
    fetch: async (request) => {
      const { pathname } = new URL(request.url);
      const channel = /^\/channels\/([^/]+)\/events$/.exec(pathname);
      if (channel === null)
        return pathname.startsWith("/v1/")
          ? api(ctx, options.authenticate, request)
          : failure("not_found", `no route ${pathname}`);
      const name = decodeURIComponent(channel[1] ?? "");
      if (request.method === "GET") return challenge(ctx, name, request);
      if (request.method !== "POST")
        return failure("not_found", "webhooks are GET or POST");
      const received = await receive(ctx, name, request);
      if (timer !== undefined) kickAll(received.threads);
      return received.response;
    },
    startRun: (request, { principal, idempotencyKey }) =>
      startRun(ctx, request, principal, idempotencyKey),
    subscribe: (threadId, runId, { principal, afterSeq }) =>
      subscribe(ctx, threadId, runId, principal, afterSeq),
    ready: async () => {
      checkBindings(ctx);
      const bound = bindSchedules(ctx, options.schedules ?? []);
      if (typeof bound === "string")
        throw new ConfigError("invalid_config", bound);
      await storeConnection(ctx.store);
      const startedAt = Date.now();
      timer = setInterval(() => {
        if (ticking !== undefined) return;
        const waiting = tickWaiters;
        tickWaiters = [];
        ticking = (async (): Promise<void> => {
          try {
            await tick(ctx, bound, startedAt, Date.now());
            await recoverReplies();
            await sweep();
          } catch (error) {
            console.error("threads host: tick failed", error);
          } finally {
            ticking = undefined;
            for (const resolve of waiting) resolve();
          }
        })();
      }, TICK_MS);
    },
    stop,
    [Symbol.asyncDispose]: stop,
  };
  TICKS.set(made, () => {
    const next = Promise.withResolvers<void>();
    tickWaiters.push(next.resolve);
    return next.promise;
  });
  SANDBOXES.set(
    made,
    [...ctx.agents.values()].flatMap((a) => a.runner.sandbox ?? []),
  );
  return made;
}

/** Every channel routes to a host agent, and its secrets resolve (missing_secret otherwise). */
function checkBindings(ctx: HostContext): void {
  for (const [name, adapter] of ctx.channels) {
    if (!ctx.agents.has(adapter.agent))
      throw new ConfigError(
        "invalid_config",
        `channel ${name} routes to ${adapter.agent}, which is not a host agent`,
      );
    for (const secret of Object.values(adapter.secrets)) secret.reveal();
  }
}
