import type { Principal } from "../log";
import type { Sandbox } from "../sandbox";
import type { EventDraft } from "../store";
import { execute } from "./execute";
import type { RunResult, ThreadRef } from "./result";
import type { Hooks, RunOptions } from "./run";
import { pinnedAfterSetup, putSpecs, type Resolved } from "./run";
import type { Store } from "./sqlite";
import { leadStarted } from "./team/runtime";

// What the host (@threads/host) needs from an agent handle beyond run(): the thread_started a new
// thread of it opens with, so the host can make a run's first input durable together with its
// own rows (an idempotency receipt, an inbox item) before the loop starts; and an execution
// that takes whatever inputs the host already recorded, or none, to resume a parked branch.

export type HostRunner = {
  /**
   * The pinned thread_started for a new thread of this agent, its deferred tools' spec artifacts
   * already put in `store` (they must be durable before it is appended). Throws ConfigError.
   */
  readonly started: (store: Store) => Promise<EventDraft>;
  /** Runs the branch until idle or parked: `inputs` are appended in order, each once idle. */
  readonly execute: (
    plan: {
      readonly store: Store;
      readonly principal: Principal;
      readonly thread: ThreadRef;
      readonly signal?: AbortSignal;
      readonly ceiling?: NonNullable<RunOptions<unknown>["ceiling"]>;
    },
    inputs: readonly EventDraft[],
    hooks?: Hooks,
  ) => Promise<RunResult<unknown>>;
  /** agent({approvers}): who may answer approval challenges; undefined when not configured. */
  readonly approvers: readonly Principal[] | undefined;
  /** The provider fork() restores snapshots with. */
  readonly sandbox: Sandbox | undefined;
  /** agent({handoffs}): a channel conversation continues with the target after a handoff. */
  readonly targets: readonly { readonly name: string }[];
};

export function hosted<Deps, Output>(
  def: Resolved<Deps, Output>,
  approvers: readonly Principal[] | undefined,
): HostRunner {
  return {
    // A lead's first append, whoever makes it (a run, a channel, a schedule), opens its team.
    started: async (store) => {
      const pinned = await pinnedAfterSetup(def);
      await putSpecs(store, pinned);
      return leadStarted(def, pinned.started, Date.now());
    },
    execute: (plan, inputs, hooks = {}) => execute(def, plan, inputs, hooks),
    approvers,
    sandbox: def.sandbox,
    targets: def.targets,
  };
}
