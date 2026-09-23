import type { Principal } from "../log";
import type { Subagent } from "../loop";
import type { Store } from "./sqlite";

// Agent handles are plain objects (spec/api.json Agent); what a parent needs to run one as a
// child is kept here, keyed by the handle, so the public shape stays small.

/** What a child run shares with its parent's run. */
export type ChildEnv = {
  readonly store: Store;
  /** The originating principal: a child never creates a new caller. */
  readonly principal: Principal;
  readonly signal?: AbortSignal;
};

export type ChildFactory = (env: ChildEnv) => Subagent;

const CHILDREN = new WeakMap<object, ChildFactory>();

export function register(agent: object, factory: ChildFactory): void {
  CHILDREN.set(agent, factory);
}

export function childFactory(agent: object): ChildFactory | undefined {
  return CHILDREN.get(agent);
}
