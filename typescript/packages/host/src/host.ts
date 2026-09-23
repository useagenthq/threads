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
import { type RunAccepted, type StartRunCode, startRun } from "./runs";
import { bindSchedules, type Schedule, tick } from "./schedules";
import type { StartRunRequest } from "./schemas";
import { type SseMessage, subscribe } from "./subscribe";

// host() (spec/api.json): binds agents to a store, channels
// and schedules. It starts nothing until ready(), which confirms the bindings, sends nothing and
// starts no run; after it, the host consumes durable intake and fires due schedules. stop()
// drains in-flight work and releases leases.

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
  /** Channel threads a crash may have left mid-run or owing replies, not yet settled. */
  let unreplied: Map<string, { tenant: string; id: ThreadId }> | undefined;

  /** One consumer per thread in this process; a kick while one runs is picked up by it. */
  const kick = (tenant: string, threadId: string): void => {
    if (consuming.has(threadId)) return;
    const running = (async (): Promise<void> => {
      try {
        await consume(ctx, tenant, ThreadId.parse(threadId));
      } catch (error) {
        console.error(`threads host: consuming ${threadId} failed`, error);
      } finally {
        consuming.delete(threadId);
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
    const { db } = await storeConnection(ctx.store);
    unreplied ??= new Map(
      channelThreads(db).map((r) => [
        r.thread_id,
        { tenant: r.tenant_id, id: r.thread_id },
      ]),
    );
    for (const [key, t] of unreplied) {
      const { log } = await ctx.open(t.tenant);
      const main = log.mainBranch(t.id);
      const done =
        !main.ok ||
        (await ctx.recover(t.tenant, { id: t.id, branch: main.value })) ===
          "done";
      if (done) unreplied.delete(key);
    }
  };

  const stop = async (): Promise<void> => {
    clearInterval(timer);
    timer = undefined;
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
        ticking = (async (): Promise<void> => {
          try {
            await tick(ctx, bound, startedAt, Date.now());
            await recoverReplies();
            await sweep();
          } catch (error) {
            console.error("threads host: tick failed", error);
          } finally {
            ticking = undefined;
          }
        })();
      }, TICK_MS);
    },
    stop,
    [Symbol.asyncDispose]: stop,
  };
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
