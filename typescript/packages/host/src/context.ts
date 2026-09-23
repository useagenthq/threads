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
  /** Each branch's last run that threw here, with its error: why recovery gives up on it. */
  readonly #thrown = new Map<BranchId, unknown>();
  /** Branches recovery ran that failed another way than losing the lease: not run again. */
  readonly #notRetried = new Set<BranchId>();
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
  resume(
    hosted: HostedAgent,
    tenant: string,
    principal: Principal,
    thread: { readonly id: ThreadId; readonly branch: BranchId },
    runId?: string,
  ): Promise<RunResult<Json> | undefined> {
    return this.#queued(thread.branch, async () => {
      if (this.stopping.aborted) return undefined;
      const store = this.storeFor(tenant);
      this.#thrown.delete(thread.branch);
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
        return json;
      } catch (error) {
        console.error(`threads host: run on ${thread.branch} failed`, error);
        this.#thrown.set(thread.branch, error);
        return undefined;
      }
    });
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

  /**
   * A pass over a channel thread no job of this process is on: a turn left open with no run
   * (a crash, or a lease lost to another host) runs on from the log, then the thread's missing
   * replies are issued. "busy" when it must be looked at again on a later tick. It never waits
   * on a run: a live one replies as it ends, and a slow one must not hold up other threads or
   * stop().
   */
  async recover(
    tenant: string,
    thread: { readonly id: ThreadId; readonly branch: BranchId },
  ): Promise<"done" | "busy"> {
    if (this.#pending.has(thread.branch)) return "busy";
    // Said once when it failed; the watch drops it now.
    if (this.#notRetried.delete(thread.branch)) return "done";
    const { log } = await this.open(tenant);
    const read = log.read(thread.branch);
    if (!read.ok) return "done";
    const { fold } = read.value;
    if (!fold.turnOpen || fold.parked.length > 0)
      return this.replies(tenant, thread);
    const events = knownEvents(read.value);
    const hosted = this.agentOf(events);
    const who = events.findLast((e) => e.type === "user_input")?.actor
      .principal;
    if (hosted === undefined || who === undefined) return "done";
    // The run issues its replies as it ends; a later tick confirms the turn closed.
    void this.#rerun(hosted, tenant, who, thread);
    return "busy";
  }

  /**
   * A recovery run. Only a lost lease is worth another try on a later tick: any other failure
   * is logged with its reason and not retried, and the turn stays open in the log for a control,
   * a new input or the next start.
   */
  async #rerun(
    hosted: HostedAgent,
    tenant: string,
    who: Principal,
    thread: { readonly id: ThreadId; readonly branch: BranchId },
  ): Promise<void> {
    const result = await this.resume(hosted, tenant, who, thread);
    const why =
      result === undefined
        ? this.#thrown.get(thread.branch)
        : result.status === "failed" && result.error.code !== "branch_busy"
          ? `${result.error.code}: ${result.error.message}`
          : undefined;
    if (why === undefined) return;
    this.#notRetried.add(thread.branch);
    console.error(
      `threads host: run on ${thread.branch} not retried (${why instanceof Error ? why.message : String(why)})`,
    );
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
