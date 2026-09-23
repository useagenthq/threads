import type { Agent, ChannelAdapter } from "@threads/core";
import {
  type BranchId,
  type HostRunner,
  hostRunner,
  JsonValue,
  type KnownEvent,
  openStore,
  type Principal,
  principalKey,
  type RunResult,
  type Store,
  type ThreadId,
  tenantStore,
} from "@threads/core/host";
import type { z } from "zod";
import { reply } from "./outbound";

// What every part of one host shares: its agents (by key and by pinned name), channels, and the
// in-process executions, one lane per branch so this process never races itself for a lease.

type Json = z.infer<typeof JsonValue>;

export type HostCeiling = NonNullable<
  Parameters<HostRunner["execute"]>[0]["ceiling"]
>;

export type HostedAgent = {
  readonly key: string;
  readonly agent: Agent<never, unknown>;
  readonly runner: HostRunner;
};

export class HostContext {
  readonly store: Store;
  readonly agents: ReadonlyMap<string, HostedAgent>;
  readonly channels: ReadonlyMap<string, ChannelAdapter>;
  readonly ceiling: HostCeiling | undefined;
  /** The last in-process job per branch (a run, then its replies); a new one waits for it. */
  readonly #lanes = new Map<string, Promise<unknown>>();
  /** A run's in-process result, by run_id, for a halt the log can't show. */
  readonly results: Map<string, RunResult<Json>> = new Map();
  readonly #aborts = new Set<AbortController>();
  #stopped = false;

  constructor(
    store: Store,
    agents: Readonly<Record<string, Agent<never, unknown>>>,
    channels: Readonly<Record<string, ChannelAdapter>>,
    ceiling?: HostCeiling,
  ) {
    this.store = store;
    this.ceiling = ceiling;
    this.agents = new Map(
      Object.entries(agents).map(([key, agent]) => {
        const runner = hostRunner(agent);
        if (runner === undefined)
          throw new Error(`host agent ${key} was not made by agent()`);
        return [key, { key, agent, runner }];
      }),
    );
    this.channels = new Map(Object.entries(channels));
  }

  /** The store as `tenant` sees it. */
  storeFor(tenant: string): Store {
    return tenantStore(this.store, tenant);
  }

  /** The agent a thread was started with, by its pinned agent_name. */
  agentOf(events: readonly KnownEvent[]): HostedAgent | undefined {
    const started = events.find((e) => e.type === "thread_started");
    if (started?.type !== "thread_started") return undefined;
    return [...this.agents.values()].find(
      (a) => a.agent.name === started.data.agent_name,
    );
  }

  /**
   * Runs the branch until idle or parked, after any execution this process already has on it.
   * The log decides what runs: the inputs are already durable.
   */
  resume(
    hosted: HostedAgent,
    tenant: string,
    principal: Principal,
    thread: { readonly id: ThreadId; readonly branch: BranchId },
    runId?: string,
  ): Promise<RunResult<Json> | undefined> {
    const prior = this.#lanes.get(thread.branch) ?? Promise.resolve(undefined);
    const next = (async (): Promise<RunResult<Json> | undefined> => {
      await prior;
      if (this.#stopped) return undefined;
      const store = this.storeFor(tenant);
      const abort = new AbortController();
      this.#aborts.add(abort);
      try {
        const result = await hosted.runner.execute(
          {
            store,
            principal,
            thread: { ...thread, store },
            signal: abort.signal,
            ...(this.ceiling === undefined ? {} : { ceiling: this.ceiling }),
          },
          [],
        );
        const json = asJson(result);
        if (runId !== undefined) this.results.set(runId, json);
        await this.#reply(tenant, thread);
        return json;
      } catch (error) {
        console.error(`threads host: run on ${thread.branch} failed`, error);
        return undefined;
      } finally {
        this.#aborts.delete(abort);
      }
    })();
    this.#lanes.set(thread.branch, next);
    return next;
  }

  /** The thread's missing replies, issued after any job this process has on its branch. */
  replies(
    tenant: string,
    thread: { readonly id: ThreadId; readonly branch: BranchId },
  ): Promise<"done" | "busy"> {
    const prior = this.#lanes.get(thread.branch) ?? Promise.resolve(undefined);
    const next = (async (): Promise<"done" | "busy"> => {
      await prior;
      return this.#stopped ? "busy" : this.#reply(tenant, thread);
    })();
    this.#lanes.set(thread.branch, next);
    return next;
  }

  async #reply(
    tenant: string,
    thread: { readonly id: ThreadId; readonly branch: BranchId },
  ): Promise<"done" | "busy"> {
    try {
      return await reply(this, tenant, thread.id);
    } catch (error) {
      console.error(`threads host: replies on ${thread.branch} failed`, error);
      return "busy";
    }
  }

  /** Waits for every in-process execution; later resumes start nothing. */
  async stop(): Promise<void> {
    this.#stopped = true;
    for (const abort of this.#aborts) abort.abort();
    await Promise.all(this.#lanes.values());
  }

  /** Whether `principal` may answer this agent's challenges. */
  mayApprove(
    hosted: HostedAgent | undefined,
    principal: Principal,
    via: "api" | "channel",
  ): boolean {
    const approvers = hosted?.runner.approvers;
    // Unconfigured: the host API's authenticated principals, and nobody over a channel.
    if (approvers === undefined) return via === "api";
    const key = principalKey(principal);
    return approvers.some((a) => principalKey(a) === key);
  }

  async open(tenant: string): Promise<Awaited<ReturnType<typeof openStore>>> {
    return openStore(this.storeFor(tenant));
  }
}

/** A run's result with its Output as JSON: the host serves data, never a typed Output. */
function asJson(result: RunResult<unknown>): RunResult<Json> {
  if (result.status !== "completed") return result;
  return { ...result, output: toJson(result.output) };
}

function toJson(value: unknown): Json {
  return value === undefined ? null : JsonValue.parse(value);
}
