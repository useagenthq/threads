import type { Agent, ChannelAdapter } from "@threads/core";
import {
  type BranchId,
  type HostRunner,
  hostRunner,
  JsonValue,
  type KnownEvent,
  knownEvents,
  openStore,
  type Principal,
  principalKey,
  type RunResult,
  type Store,
  StoreError,
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
  readonly name: string;
  readonly runner: HostRunner;
};

export class HostContext {
  readonly store: Store;
  readonly agents: ReadonlyMap<string, HostedAgent>;
  readonly channels: ReadonlyMap<string, ChannelAdapter>;
  readonly ceiling: HostCeiling | undefined;
  /** The last in-process job per branch (a run, then its replies); a new one waits for it. */
  readonly #lanes = new Map<string, Promise<unknown>>();
  /** How many jobs each branch's lane has queued or running. */
  readonly #pending = new Map<string, number>();
  /** A run's in-process result, by run_id, for a halt the log can't show. */
  readonly results: Map<string, RunResult<Json>> = new Map();
  /**
   * Told when a run of this process meets a store outage: the host watches the thread, so its
   * recovery runs it on with backoff. Unset, the outage is logged like any failure.
   */
  onStoreOutage:
    | ((tenant: string, thread: Thread, error: StoreError) => void)
    | undefined;
  readonly #stop = new AbortController();
  /** Aborted once the host stops: every run and every send not yet settled gives up on it. */
  readonly stopping: AbortSignal = this.#stop.signal;

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
        return [key, { key, name: agent.name, runner }];
      }),
    );
    this.channels = new Map(Object.entries(channels));
  }

  /** The store as `tenant` sees it. */
  storeFor(tenant: string): Store {
    return tenantStore(this.store, tenant);
  }

  /**
   * The agent a thread was started with, by its pinned agent_name: a host agent, else a handoff
   * target one of them names (directly or through other targets), so a channel conversation that
   * was handed off continues with the target.
   */
  agentOf(events: readonly KnownEvent[]): HostedAgent | undefined {
    const started = events.find((e) => e.type === "thread_started");
    if (started?.type !== "thread_started") return undefined;
    const name = started.data.agent_name;
    const queue = [...this.agents.values()];
    const seen = new Set<HostRunner>();
    for (const hosted of queue) {
      if (hosted.name === name) return hosted;
      if (seen.has(hosted.runner)) continue;
      seen.add(hosted.runner);
      for (const target of hosted.runner.targets) {
        const runner = hostRunner(target);
        if (runner !== undefined)
          queue.push({ key: hosted.key, name: target.name, runner });
      }
    }
    return undefined;
  }

  /**
   * Runs the branch until idle or parked, after any execution this process already has on it.
   * The log decides what runs: the inputs are already durable.
   */
  async resume(
    hosted: HostedAgent,
    tenant: string,
    principal: Principal,
    thread: Thread,
    runId?: string,
  ): Promise<RunResult<Json> | undefined> {
    const ran = await this.attempt(hosted, tenant, principal, thread, runId);
    if (ran.kind === "threw") {
      if (ran.error instanceof StoreError && this.onStoreOutage !== undefined)
        this.onStoreOutage(tenant, thread, ran.error);
      else
        console.error(
          `threads host: run on ${thread.branch} failed`,
          ran.error,
        );
    }
    return ran.kind === "ran" ? ran.result : undefined;
  }

  /** `resume` without its failure log: how the run ended, for recovery to judge. */
  attempt(
    hosted: HostedAgent,
    tenant: string,
    principal: Principal,
    thread: Thread,
    runId?: string,
  ): Promise<Attempt> {
    return this.#queued(thread.branch, async (): Promise<Attempt> => {
      if (this.stopping.aborted) return { kind: "stopped" };
      const store = this.storeFor(tenant);
      try {
        // A reply begun before a crash is reconciled through the channel's lookup first: the
        // agent's recovery has no channel_send tool and would park it.
        await this.#reply(tenant, thread);
        const result = await hosted.runner.execute(
          {
            store,
            principal,
            thread: { ...thread, store },
            signal: this.stopping,
            ...(this.ceiling === undefined ? {} : { ceiling: this.ceiling }),
          },
          [],
        );
        const json = asJson(result);
        // A run that lost the branch is no answer: the run holding it answers from the log.
        const lost =
          json.status === "failed" && json.error.code === "branch_busy";
        if (runId !== undefined && !lost) this.results.set(runId, json);
        await this.#reply(tenant, thread);
        return { kind: "ran", result: json };
      } catch (error) {
        return { kind: "threw", error };
      }
    });
  }

  /** Whether this process has a job queued or running on the branch. */
  busy(branch: BranchId): boolean {
    return this.#pending.has(branch);
  }

  /** The thread's missing replies, issued after any job this process has on its branch. */
  replies(
    tenant: string,
    thread: { readonly id: ThreadId; readonly branch: BranchId },
  ): Promise<"done" | "busy"> {
    return this.#queued(thread.branch, async () =>
      this.stopping.aborted ? "busy" : this.#reply(tenant, thread),
    );
  }

  /** `job` after every job this process already has on the branch; counted until it ends. */
  #queued<T>(branch: BranchId, job: () => Promise<T>): Promise<T> {
    const prior = this.#lanes.get(branch) ?? Promise.resolve(undefined);
    this.#pending.set(branch, (this.#pending.get(branch) ?? 0) + 1);
    const next = (async (): Promise<T> => {
      try {
        await prior;
        return await job();
      } finally {
        const left = (this.#pending.get(branch) ?? 1) - 1;
        if (left === 0) this.#pending.delete(branch);
        else this.#pending.set(branch, left);
      }
    })();
    this.#lanes.set(branch, next);
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

  /** Aborts every in-process run; later resumes and replies start nothing. */
  abort(): void {
    this.#stop.abort();
  }

  /** Aborts, then waits for every in-process execution. */
  async stop(): Promise<void> {
    this.abort();
    await Promise.all(this.#lanes.values());
  }

  /**
   * Whether `principal` has approval authority on this thread (spec/schema/README.md, "Approval
   * authority"): the root run's approver set, reached by following thread_started.parent, so a
   * descendant's own route never widens it. Unconfigured, only the root's originating principal.
   */
  async mayApprove(
    tenant: string,
    threadId: ThreadId,
    principal: Principal,
  ): Promise<boolean> {
    const root = await this.#root(tenant, threadId);
    if (root === undefined) return false;
    const key = principalKey(principal);
    const approvers = this.agentOf(root)?.runner.approvers;
    if (approvers !== undefined)
      return approvers.some((a) => principalKey(a) === key);
    const origin = root.findLast((e) => e.type === "user_input")?.actor
      .principal;
    return origin !== undefined && principalKey(origin) === key;
  }

  async #root(
    tenant: string,
    threadId: ThreadId,
  ): Promise<readonly KnownEvent[] | undefined> {
    const { log } = await this.open(tenant);
    const main = log.mainBranch(threadId);
    let branch = main.ok ? main.value : undefined;
    while (branch !== undefined) {
      const read = log.read(branch);
      if (!read.ok) return undefined;
      const events = knownEvents(read.value);
      const started = events.find((e) => e.type === "thread_started");
      const parent =
        started?.type === "thread_started" ? started.data.parent : undefined;
      if (parent === undefined) return events;
      branch = parent.branch_id;
    }
    return undefined;
  }

  async open(tenant: string): Promise<Awaited<ReturnType<typeof openStore>>> {
    return openStore(this.storeFor(tenant));
  }
}

type Thread = { readonly id: ThreadId; readonly branch: BranchId };

/** How one run of this process ended: its result, what it threw, or the host was stopping. */
export type Attempt =
  | { readonly kind: "ran"; readonly result: RunResult<Json> }
  | { readonly kind: "threw"; readonly error: unknown }
  | { readonly kind: "stopped" };

/** A pin never changes in place: continuing a thread needs the config it started with. */
export async function samePin(
  events: readonly KnownEvent[],
  hosted: HostedAgent,
): Promise<boolean> {
  const started = events.find((e) => e.type === "thread_started");
  const pinned = await hosted.runner.started();
  return (
    started?.type === "thread_started" &&
    pinned.type === "thread_started" &&
    started.data.config_hash === pinned.data.config_hash
  );
}

/** A run's result with its Output as JSON: the host serves data, never a typed Output. */
function asJson(result: RunResult<unknown>): RunResult<Json> {
  if (result.status !== "completed") return result;
  return { ...result, output: toJson(result.output) };
}

function toJson(value: unknown): Json {
  return value === undefined ? null : JsonValue.parse(value);
}
