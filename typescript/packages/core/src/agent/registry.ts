import type { z } from "zod";
import type {
  ArtifactRef,
  Budget,
  EventId,
  Policy,
  Principal,
  ThreadId,
} from "../log";
import type { ChildRun, Covering, Subagent } from "../loop";
import type { Thread } from "../thread/handle";
import type { HostRunner } from "./hosted";
import type { ThreadRef } from "./result";
import type { Store } from "./sqlite";

// Agent handles are plain objects (spec/api.json Agent); what a parent needs to run one as a
// subagent or a handoff target is kept here, keyed by the handle, so the public shape stays small.

/** What a thread started by another run shares with that run. */
export type ChildEnv = {
  readonly store: Store;
  /** The originating principal: neither a child nor a handoff creates a new caller. */
  readonly principal: Principal;
  readonly signal?: AbortSignal;
  /** A handoff target's ceilings: those of the run that handed off. */
  readonly ceilings?: readonly NonNullable<Policy["permissions"]>[];
  /** A subagent's handoff target: the handing-off child's decision chain (Handoff scope). */
  readonly chain?: ChildRun["ceiling"];
  /** A handoff target: every budget covering the handing-off thread, as an ancestor's. */
  readonly covering?: readonly Covering[];
};

export type ChildFactory = (env: ChildEnv) => Subagent;

/** The handoff a target thread starts from. */
export type HandoffLink = {
  readonly threadId: ThreadId;
  readonly parent: {
    readonly thread_id: ThreadId;
    readonly branch_id: ThreadRef["branch"];
    readonly event_id: EventId;
    readonly relation: "handoff";
  };
  readonly forwarded?: ArtifactRef;
  readonly request: string;
};

export type TargetFactory = (
  env: ChildEnv,
) => (link: HandoffLink) => Promise<Thread>;

/** Setup's check of an agent's tree under the limits covering it. */
export type Enforcement = (covering: readonly z.infer<typeof Budget>[]) => void;

type Entry = {
  /** The agent's setup (agent/setup.ts), run by a parent's setup too, with the agents it walked. */
  readonly setup: (walked?: Set<object>) => Promise<void>;
  readonly child: ChildFactory;
  readonly target: TargetFactory;
  readonly enforce: Enforcement;
  readonly host?: HostRunner;
};

const AGENTS = new WeakMap<object, Entry>();

export function register(agent: object, entry: Entry): void {
  AGENTS.set(agent, entry);
}

export function setupOf(
  agent: object,
): ((walked?: Set<object>) => Promise<void>) | undefined {
  return AGENTS.get(agent)?.setup;
}

export function childFactory(agent: object): ChildFactory | undefined {
  return AGENTS.get(agent)?.child;
}

/** The host's view of an agent handle (@threads/host), or undefined for a foreign object. */
export function hostRunner(agent: object): HostRunner | undefined {
  return AGENTS.get(agent)?.host;
}

export function enforcement(agent: object): Enforcement | undefined {
  return AGENTS.get(agent)?.enforce;
}

export function targetFactory(agent: object): TargetFactory | undefined {
  return AGENTS.get(agent)?.target;
}
