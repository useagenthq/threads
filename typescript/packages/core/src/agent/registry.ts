import type { z } from "zod";
import type { EventOf } from "../fold/state";
import type {
  ArtifactRef,
  Budget,
  EventId,
  Policy,
  Principal,
  ThreadId,
} from "../log";
import type { ChildRun, Covering, Subagent, TeamAgentPin } from "../loop";
import type { DynamicChoice } from "../team/dynamic";
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

/** What a team needs of an agent it lists (agent({team})). */
export type MemberEntry = {
  /** Its handoffs: a member can't hand off (handoff_in_team). */
  readonly handsOff: boolean;
  /** Its own tools' names: none may be a team tool's (duplicate_name). */
  readonly toolNames: readonly string[];
  /** The agents of its own team, when it leads one (a nested lead). */
  readonly team: readonly object[] | undefined;
  /** A dynamic agent (lane 26): its model keys, and the choosable tools a lead's listing shows. */
  readonly template?: {
    readonly models: readonly string[];
    readonly listed: () => readonly string[];
  };
  /**
   * Its pin as a team member, after setup: config_hash and the canonical config; a dynamic
   * agent's as the member a choice defines. Throws ConfigError.
   */
  readonly pinned: (choice?: DynamicChoice) => Promise<TeamAgentPin>;
  /** Runs one member branch of it until the branch is idle, parked or ended. */
  readonly run: (env: MemberEnv) => Promise<void>;
};

/** One member branch, as the team worker runs it. */
export type MemberEnv = {
  readonly store: Store;
  readonly thread: ThreadRef;
  /** The member's parent: the lead's member_started (or thread_started). */
  readonly parent: EventOf<"member_started">["data"]["parent"];
  /** The principal of the member's task: its turns' actor. */
  readonly principal: Principal;
  readonly holder: string;
  readonly notify: () => void;
  /** Every ancestor thread's budget, which covers the member too. */
  readonly covering: readonly Covering[];
  readonly signal?: AbortSignal;
  /** A dynamic agent's member: what its starter chose. */
  readonly dynamic?: DynamicChoice;
};

type Entry = {
  /** The agent's setup (agent/setup.ts), run by a parent's setup too, with the agents it walked. */
  readonly setup: (walked?: Set<object>) => Promise<void>;
  readonly child: ChildFactory;
  readonly target: TargetFactory;
  readonly enforce: Enforcement;
  readonly host?: HostRunner;
  readonly member: MemberEntry;
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

/** What a team needs of an agent it lists, or undefined for a foreign object. */
export function memberEntry(agent: object): MemberEntry | undefined {
  return AGENTS.get(agent)?.member;
}

export function enforcement(agent: object): Enforcement | undefined {
  return AGENTS.get(agent)?.enforce;
}

export function targetFactory(agent: object): TargetFactory | undefined {
  return AGENTS.get(agent)?.target;
}
