import type { ArtifactRef, EventId, Policy, Principal, ThreadId } from "../log";
import type { Subagent } from "../loop";
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
) => (link: HandoffLink) => Promise<ThreadRef>;

type Entry = {
  readonly child: ChildFactory;
  readonly target: TargetFactory;
  readonly host?: HostRunner;
};

const AGENTS = new WeakMap<object, Entry>();

export function register(agent: object, entry: Entry): void {
  AGENTS.set(agent, entry);
}

export function childFactory(agent: object): ChildFactory | undefined {
  return AGENTS.get(agent)?.child;
}

/** The host's view of an agent handle (@threads/host), or undefined for a foreign object. */
export function hostRunner(agent: object): HostRunner | undefined {
  return AGENTS.get(agent)?.host;
}

export function targetFactory(agent: object): TargetFactory | undefined {
  return AGENTS.get(agent)?.target;
}
