import type { z } from "zod";
import type {
  InputPart,
  KnownEvent,
  PermissionsPolicy,
  Principal,
  ThreadId,
} from "../log";
import type { ChildRun, Covering, StubGateway } from "../loop";
import { DEFAULT_PERMISSIONS } from "../permissions";
import type { EventDraft } from "../store";
import type { Agent, Models } from "./agent";
import { type DeferTools, inheritDefer, storeSpecs } from "./defer";
import { checkTree } from "./enforceable";
import { execute } from "./execute";
import type { Extension } from "./extension";
import type { ChildPin, PinOptions } from "./pin";
import { pin } from "./pin";
import type { MemberEnv } from "./registry";
import type { Decode, RunResult, ThreadRef } from "./result";
import { connectAll, type McpServer } from "./setup";
import { openStore, type Store, sqlite } from "./sqlite";
import type { DynamicAgent } from "./team/types";
import type { Tool } from "./tool";

// One run: take the branch lease (recovery runs first), record the input, and loop until the
// branch is idle or parked. Needs no server.

/** spec/api.json Agent.run options (TS casing). */
export type RunOptions<Deps> = {
  readonly thread?: ThreadId | ThreadRef;
  readonly store?: Store;
  readonly deps?: Deps;
  readonly budget?: NonNullable<PinOptions["budget"]>;
  readonly principal?: Principal;
  readonly signal?: AbortSignal;
  /** The principal and host ceiling (spec/api.json Agent.run ceiling). */
  readonly ceiling?: Partial<Permissions>;
};

type Permissions = z.infer<typeof PermissionsPolicy>;

export type Resolved<Deps, Output> = Omit<PinOptions, "mcp"> & {
  readonly bindable: readonly Tool<unknown, unknown, Deps>[];
  readonly hookable: readonly Extension<Deps>[];
  /** Setup of this agent and those it may start (agent/setup.ts); retried after a failure. */
  readonly setup: (walked?: Set<object>) => Promise<void>;
  /** MCP servers: each check() and each run opens its own sessions. */
  readonly servers: readonly McpServer[];
  readonly decode: Decode<Output>;
  /** The agents behind PinOptions.subagents, by name. */
  readonly agents: readonly Agent<never, unknown>[];
  /** The agents behind PinOptions.handoffs, by name. */
  readonly targets: readonly Agent<never, unknown>[];
  /** The agents behind PinOptions.team, by name: those start may name. */
  readonly members: readonly (
    | Agent<never, unknown>
    | DynamicAgent<never, unknown>
  )[];
  /** A dynamic agent (dynamicAgent): the models a start may choose, the first its default. */
  readonly models?: Models;
  /** agent({teamLimits}), defaults filled in. */
  readonly teamLimits: {
    readonly concurrent: number;
    readonly mailbox: number;
  };
};

/** An agent with its MCP servers' tools, bound to one session. */
export type SetUp<Deps, Output> = Resolved<Deps, Output> &
  Pick<PinOptions, "mcp">;

/** The local operator. */
const OPERATOR: Principal = {
  issuer: "api",
  tenant: "local",
  subject: "operator",
};

export type Hooks = {
  readonly onEvent?: (event: KnownEvent) => void;
  readonly onDelta?: (
    requestEventId: string,
    part: number,
    text: string,
  ) => void;
};

export async function run<Deps, Output>(
  def: Resolved<Deps, Output>,
  input: string | readonly InputPart[],
  options: RunOptions<Deps>,
  hooks: Hooks = {},
  stub?: StubGateway,
): Promise<RunResult<Output>> {
  checkTree(def, options.budget === undefined ? [] : [options.budget]);
  const principal = options.principal ?? OPERATOR;
  const draft: EventDraft = {
    type: "user_input",
    type_version: 1,
    critical: true,
    actor: { kind: "user", principal },
    data: {
      source: "api",
      ...(typeof input === "string"
        ? { text: input }
        : { content: [...input] }),
      ...(options.budget === undefined ? {} : { budget: options.budget }),
    },
  };
  const store =
    options.store ?? handleOf(options.thread)?.store ?? sqlite(".threads");
  return execute(
    def,
    { ...options, store, principal, ...(stub === undefined ? {} : { stub }) },
    [draft],
    hooks,
  );
}

/** What one execution runs besides the agent: a thread, and for a child its parent's link. */
export type Plan<Deps> = RunOptions<Deps> & {
  readonly store: Store;
  readonly principal: Principal;
  /** A child thread, opened by its id and created on first use. */
  readonly child?: ChildRun;
  /** A handoff target's thread, likewise. */
  readonly target?: Target;
  /** A team member's branch, opened at materialize and run by the team worker. */
  readonly member?: Pick<MemberEnv, "parent" | "notify">;
  /** The lease holder; a new one by default, so a second run on a busy branch is branch_busy. */
  readonly holder?: string;
  /** A handoff target's ceilings: the handing-off run's, never the source agent's policy. */
  readonly ceilings?: readonly Permissions[];
  /** A handoff target of a subagent: the handing-off child's decision, every ancestor's included. */
  readonly chain?: ChildRun["ceiling"];
  /**
   * true: a host started the run for someone who can answer, so a new thread pins ask_user;
   * "pinned": continue a thread as its pin says (the host's resume).
   */
  readonly answerer?: boolean | "pinned";
  /** A handoff target: every budget covering the handing-off thread, as an ancestor's. */
  readonly covering?: readonly Covering[];
  /** A handoff target's or member's parent's resolved defer_tools, unless it sets its own. */
  readonly deferTools?: DeferTools;
  /** A live eval's recorded stubs: every mediated call of the tree answers from them. */
  readonly stub?: StubGateway;
};

/** Every ceiling this run is also decided under. */
export function ceilingsOf(plan: Plan<unknown>): readonly Permissions[] {
  if (plan.ceilings !== undefined) return plan.ceilings;
  return plan.ceiling === undefined
    ? []
    : [{ ...DEFAULT_PERMISSIONS, ...plan.ceiling }];
}

/** A handoff target: created on first use with the forwarded history after thread_started. */
export type Target = {
  readonly threadId: ThreadId;
  readonly parent: ChildPin["parent"];
  readonly prefix: readonly EventDraft[];
};

/**
 * The pin of a new thread of this agent, after setup, with its MCP servers' tools listed on
 * sessions that are closed before it returns; as a team member's with `member`. Throws
 * ConfigError.
 */
export async function pinnedAfterSetup<Deps, Output>(
  def: Resolved<Deps, Output>,
  member = false,
  deferTools?: DeferTools,
  answerer = false,
): Promise<ReturnType<typeof pin>> {
  await def.setup();
  await using mcp = await connectAll(def.servers);
  const context = inheritDefer(def.context, deferTools);
  return pin({ ...def, context, mcp: mcp.tools }, undefined, member, answerer);
}

/** Whether a thread's pin offers ask_user: a host started it for someone who can answer. */
export function asks(events: readonly KnownEvent[]): boolean {
  const started = events.find((e) => e.type === "thread_started");
  return (
    started?.type === "thread_started" &&
    started.data.tools.some((t) => t.name === "ask_user")
  );
}

/** Puts a pin's spec artifacts: before any append of its thread_started. */
export async function putSpecs(
  store: Store,
  pinned: Pick<ReturnType<typeof pin>, "artifacts">,
): Promise<void> {
  const { artifacts } = await openStore(store);
  storeSpecs(artifacts, pinned.artifacts);
}

/** Budgets covering this thread as an ancestor's: a child's parent's, a target's source's. */
export function inheritedOf(plan: Plan<unknown>): readonly Covering[] {
  return plan.child?.covering ?? plan.covering ?? [];
}

function handleOf(
  thread: RunOptions<unknown>["thread"],
): ThreadRef | undefined {
  return typeof thread === "object" ? thread : undefined;
}
