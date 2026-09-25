import type { z } from "zod";
import type { EventOf } from "../fold/state";
import type {
  ArtifactRef,
  Budget,
  EventId,
  Policy,
  Principal,
  ThreadId,
  ThreadStartedData,
} from "../log";
import type {
  ChildRun,
  Covering,
  StubGateway,
  Subagent,
  TeamAgentPin,
} from "../loop";
import type { DynamicChoice } from "../team/dynamic";

import type { Thread } from "../thread/handle";
import type { HostRunner } from "./hosted";
import type { RunResult, ThreadRef } from "./result";
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
  /** A handoff target: the handing-off thread's resolved defer_tools. */
  readonly deferTools?: NonNullable<Policy["context"]>["defer_tools"];
  /** A live eval's recorded stubs, which the whole tree answers mediated calls from. */
  readonly stub?: StubGateway;
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
  /**
   * Its config_hash as a thread of its own, pinned as that thread was: a nested lead's as a
   * member, a hosted one's with ask_user, with the defer_tools the thread resolved. Throws
   * ConfigError.
   */
  readonly configHash: (as: {
    readonly member: boolean;
    readonly answerer: boolean;
    readonly deferTools:
      | NonNullable<Policy["context"]>["defer_tools"]
      | undefined;
  }) => Promise<string>;
  /** agent({teamLimits}), defaults filled in: what its team's starts and sends are capped by. */
  readonly teamLimits: {
    readonly concurrent: number;
    readonly mailbox: number;
  };
  /** A dynamic agent (lane 26): its model keys, and the choosable tools a lead's listing shows. */
  readonly template?: {
    readonly models: readonly string[];
    readonly listed: () => readonly string[];
  };
  /**
   * Its pin as a team member, after setup: config_hash and the canonical config; a dynamic
   * agent's as the member a choice defines. `deferTools` is the lead's resolved defer_tools,
   * which it inherits unless it sets its own. Throws ConfigError.
   */
  readonly pinned: (
    deferTools: NonNullable<Policy["context"]>["defer_tools"] | undefined,
    choice?: DynamicChoice,
  ) => Promise<TeamAgentPin>;
  /** Runs one member branch of it until the branch is idle, parked or ended. */
  readonly run: (env: MemberEnv) => Promise<void>;
};

/** One member branch, as the team worker runs it. */
export type MemberEnv = {
  readonly store: Store;
  readonly thread: ThreadRef;
  /** The member's parent: the lead's member_started (or thread_started). */
  readonly parent: NonNullable<EventOf<"member_started">["data"]["parent"]>;
  /** The principal of the member's task: its turns' actor. */
  readonly principal: Principal;
  readonly holder: string;
  readonly notify: () => void;
  /** Every ancestor thread's budget, which covers the member too. */
  readonly covering: readonly Covering[];
  readonly signal?: AbortSignal;
  /** A dynamic agent's member: what its starter chose. */
  readonly dynamic?: DynamicChoice;
  /** The lead's resolved defer_tools, inherited unless the member sets its own. */
  readonly deferTools?: NonNullable<Policy["context"]>["defer_tools"];
};
/** A pin made without setup, secrets or MCP connections, and what it therefore couldn't see. */
export type DryPin = {
  readonly started: z.infer<typeof ThreadStartedData>;
  /** MCP servers whose tools only a connection lists. */
  readonly mcp: readonly string[];
  /** Extensions with a setup step: their tools and instructions may come from it. */
  readonly setupExtensions: readonly string[];
  /** Memory and knowledge providers with a setup step, by role. */
  readonly setupProviders: readonly ("memory" | "knowledge")[];
  /** agent({team}): its members run in the team worker, outside a live eval's stubs. */
  readonly leadsTeam: boolean;
};

/** What a live eval runs an agent with. */
export type LiveOptions = {
  readonly store: Store;
  readonly principal: Principal;
  readonly budget: z.infer<typeof Budget>;
  readonly stub: StubGateway;
};
export type LiveRun = (
  input: string,
  options: LiveOptions,
) => Promise<RunResult<unknown>>;

type Entry = {
  /** The agent's setup (agent/setup.ts), run by a parent's setup too, with the agents it walked. */
  readonly setup: (walked?: Set<object>) => Promise<void>;
  /** The agent pinned without setup (evals' drift check). Throws ConfigError on a bad config. */
  readonly dry?: () => DryPin;
  /** A live eval's run: a new thread whose mediated calls answer from recorded stubs. */
  readonly live?: LiveRun;
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

/**
 * The team leads this process defined, by name, each held weakly: openTeam rebinds a team's lead
 * by its name and config_hash among them, as materialize rebinds a member.
 */
const LEADS = new Map<string, Set<WeakRef<object>>>();

export function registerLead(name: string, lead: object): void {
  const known = LEADS.get(name) ?? new Set<WeakRef<object>>();
  known.add(new WeakRef(lead));
  LEADS.set(name, known);
}

/** Every live lead of that name, oldest first; a collected one is dropped. */
export function leadsNamed(name: string): readonly object[] {
  const known = LEADS.get(name) ?? new Set<WeakRef<object>>();
  const live: object[] = [];
  for (const ref of known) {
    const lead = ref.deref();
    if (lead === undefined) known.delete(ref);
    else live.push(lead);
  }
  return live;
}

export function setupOf(
  agent: object,
): ((walked?: Set<object>) => Promise<void>) | undefined {
  return AGENTS.get(agent)?.setup;
}

/** A live eval's run of an agent handle, or undefined for a foreign object. */
export function liveRunOf(agent: object): LiveRun | undefined {
  return AGENTS.get(agent)?.live;
}

/** The dry pin of an agent handle, or undefined for a foreign object. */
export function dryPinOf(agent: object): (() => DryPin) | undefined {
  return AGENTS.get(agent)?.dry;
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
