import type { Principal } from "../log";
import type { Sandbox } from "../sandbox";
import type { EventDraft } from "../store";
import { type MessagePolicyRule, ruleStarts } from "../team/policy";
import type { Agent } from "./agent";
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

/** A new thread's thread_started, and the spec artifacts it names. */
export type NewThreadPin = {
  readonly event: EventDraft;
  /**
   * Puts the deferred tools' spec artifacts in `store`. Call it before appending `event`, and
   * only then: a pin that is only compared writes nothing.
   */
  readonly put: (store: Store) => Promise<void>;
};

export type HostRunner = {
  /**
   * The pinned thread_started for a new thread of this agent. Throws ConfigError. `answerer`:
   * the run is for someone who can answer (a channel conversation, an authenticated API call),
   * so ask_user is pinned.
   */
  readonly started: (options?: {
    readonly answerer?: boolean;
  }) => Promise<NewThreadPin>;
  /**
   * Runs the branch until idle or parked: `inputs` are appended in order, each once idle. The
   * thread is continued as its pin says, ask_user included.
   */
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
  /**
   * The same runner under the host's messagePolicy: its rules decide this agent's team calls,
   * give it its team tools and, with a start rule, make it a lead (lane 29C). One runner per
   * host, so a thread's pin and its continuations see the same rules.
   */
  readonly withPolicy: (policy: AgentPolicy) => HostRunner;
};

/** One agent's side of host({messagePolicy}). */
export type AgentPolicy = {
  /** The rules with this agent as `from`, in configured order. */
  readonly rules: readonly MessagePolicyRule[];
  /** Every host agent, for a rule-only lead to start and rebind the ones its rules name. */
  readonly agents: readonly Agent<never, unknown>[];
};

export function hosted<Deps, Output>(
  def: Resolved<Deps, Output>,
  approvers: readonly Principal[] | undefined,
): HostRunner {
  return {
    // A lead's first append, whoever makes it (a run, a channel, a schedule), opens its team.
    started: async (options = {}) => {
      const answerer = options.answerer === true;
      const pinned = await pinnedAfterSetup(def, false, undefined, answerer);
      return {
        event: leadStarted(def, pinned.started, Date.now()),
        put: (store) => putSpecs(store, pinned),
      };
    },
    execute: (plan, inputs, hooks = {}) =>
      execute(def, { ...plan, answerer: "pinned" }, inputs, hooks),
    approvers,
    sandbox: def.sandbox,
    targets: def.targets,
    withPolicy: (policy) => hosted(ruled(def, policy), approvers),
  };
}

/**
 * The definition under one host's rules: the rules themselves, and the agents a start rule names
 * added to what start may pin, since a rule-only lead's own team is empty.
 */
function ruled<Deps, Output>(
  def: Resolved<Deps, Output>,
  policy: AgentPolicy,
): Resolved<Deps, Output> {
  if (policy.rules.length === 0) return def;
  const starts = ruleStarts(policy.rules);
  const added = policy.agents.filter(
    (a) =>
      starts.includes(a.name) && !def.members.some((m) => m.name === a.name),
  );
  return { ...def, rules: policy.rules, members: [...def.members, ...added] };
}
