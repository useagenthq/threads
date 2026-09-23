import type { z } from "zod";
import { ObserverPump } from "../hooks/observers";
import type {
  InputPart,
  KnownEvent,
  PermissionsPolicy,
  Principal,
  ThreadId,
} from "../log";
import {
  type ChildRun,
  type Covering,
  type LoopConfig,
  type LoopEnd,
  resume,
} from "../loop";
import { DEFAULT_PERMISSIONS } from "../permissions";
import { knownEvents } from "../reduce";
import { type EventDraft, keepLease, type Writer } from "../store";
import { bindBuiltins } from "../tools";
import type { LogError } from "../verify";
import type { Agent } from "./agent";
import { loopConfig } from "./config";
import { checkEnforceable } from "./enforceable";
import { ConfigError } from "./errors";
import { type Extension, observerOf } from "./extension";
import { handedOff } from "./handoff";
import { open } from "./open-thread";
import type { ChildPin, PinOptions } from "./pin";
import { pin } from "./pin";
import { bindProviders } from "./providers";
import {
  type Decode,
  type RunResult,
  runResult,
  type ThreadRef,
} from "./result";
import type { Setup } from "./setup";
import { skillTools } from "./skills";
import { snapshotTurn } from "./snapshot";
import { openStore, type Store, sqlite } from "./sqlite";
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
  readonly setup: Setup;
  readonly decode: Decode<Output>;
  /** The agents behind PinOptions.subagents, by name. */
  readonly agents: readonly Agent<never, unknown>[];
  /** The agents behind PinOptions.handoffs, by name. */
  readonly targets: readonly Agent<never, unknown>[];
};

/** An agent after setup: its MCP servers' tools are known. */
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
  readonly onDelta?: (requestEventId: string, text: string) => void;
};

export async function run<Deps, Output>(
  def: Resolved<Deps, Output>,
  input: string | readonly InputPart[],
  options: RunOptions<Deps>,
  hooks: Hooks = {},
): Promise<RunResult<Output>> {
  checkEnforceable(options.budget, [def.model, ...def.fallback]);
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
  return execute(def, { ...options, store, principal }, [draft], hooks);
}

/** What one execution runs besides the agent: a thread, and for a child its parent's link. */
export type Plan<Deps> = RunOptions<Deps> & {
  readonly store: Store;
  readonly principal: Principal;
  /** A child thread, opened by its id and created on first use. */
  readonly child?: ChildRun;
  /** A handoff target's thread, likewise. */
  readonly target?: Target;
  /** A handoff target's ceilings: the handing-off run's, never the source agent's policy. */
  readonly ceilings?: readonly Permissions[];
  /** A handoff target of a subagent: the handing-off child's decision, every ancestor's included. */
  readonly chain?: ChildRun["ceiling"];
  /** A handoff target: every budget covering the handing-off thread, as an ancestor's. */
  readonly covering?: readonly Covering[];
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
 * Takes the lease (recovery first), then appends `inputs` in order, each once the branch is
 * idle. A child's inputs are its whole history of inputs: those already in its log are skipped,
 * so a restarted parent resumes the child instead of prompting it twice.
 */
export async function execute<Deps, Output>(
  def: Resolved<Deps, Output>,
  plan: Plan<Deps>,
  inputs: readonly EventDraft[],
  hooks: Hooks = {},
): Promise<RunResult<Output>> {
  const { store, child, principal, target } = plan;
  const { log, artifacts } = await openStore(store);
  const link = linkOf(plan);
  const set: SetUp<Deps, Output> = { ...def, mcp: await def.setup() };
  const pinned = pin(set, link);
  // Each run is its own executor: a second run on a busy branch is branch_busy.
  const holder = `run-${crypto.randomUUID()}`;
  const opened = open(
    log,
    child?.threadId ?? target?.threadId ?? plan.thread,
    [pinned.started, ...(target?.prefix ?? [])],
    holder,
    link !== undefined,
  );
  const thread: ThreadRef = {
    id: opened.threadId,
    branch: opened.branchId,
    store,
  };
  if (!opened.writer.ok) return failed(opened.writer.error, thread);
  const writer = opened.writer.value;
  const stop = keepLease(writer);
  try {
    checkPin(writer, pinned.started);
    const builtin = bindBuiltins(
      child === undefined ? def.sandbox : undefined,
      def.egress,
      {
        ledger: log.ledger,
        writer,
        artifacts,
      },
      def.capabilities,
    );
    const providers = await bindProviders(def, {
      log,
      artifacts,
      principal,
      threadId: thread.id,
      events: () => knownEvents(writer.chain),
    });
    const observers = new ObserverPump(
      log.cursors,
      thread.branch,
      () => knownEvents(writer.chain),
      def.hookable.flatMap((e) => observerOf(e) ?? []),
    );
    observers.poke();
    const config = loopConfig(set, {
      options: plan,
      principal,
      thread,
      hooks: {
        ...hooks,
        onEvent: (event) => {
          hooks.onEvent?.(event);
          observers.poke();
        },
      },
      builtin: [
        ...builtin.tools,
        ...providers.tools,
        ...skillTools(def.skills),
      ],
      events: () => knownEvents(writer.chain),
      ceilings: ceilingsOf(plan),
      ...(plan.chain === undefined ? {} : { chain: plan.chain }),
      ledger: log.budgets,
      inherited: inheritedOf(plan),
      ...(builtin.readFile === undefined ? {} : { readFile: builtin.readFile }),
      ...(child === undefined ? {} : { child }),
    });
    const end = await drive(
      writer,
      artifacts,
      config,
      pendingInputs(writer, link, inputs),
    );
    if (end.kind === "idle" && child === undefined)
      await snapshotTurn(
        def.sandbox,
        builtin.session,
        log.ledger,
        writer,
        await providers.revision(),
      );
    if (end.kind === "idle" && writer.chain.fold.handedOff)
      return handedOff(def, plan, writer, thread, config.authorize);
    return runResult(
      end,
      knownEvents(writer.chain),
      writer.chain.fold.parked,
      thread,
      def.decode,
    );
  } finally {
    stop();
  }
}

/** Recovery first; an in-doubt turn finishes before the next input opens the next one. */
async function drive(
  writer: Writer,
  artifacts: Awaited<ReturnType<typeof openStore>>["artifacts"],
  config: LoopConfig,
  pending: readonly EventDraft[],
): Promise<LoopEnd> {
  let end: LoopEnd = await resume(writer, artifacts, config, {
    ...(pending[0] === undefined ? {} : { input: pending[0] }),
  });
  for (const input of pending.slice(1)) {
    if (end.kind !== "idle") break;
    end = await resume(writer, artifacts, config, { input });
  }
  return end;
}

/** Budgets covering this thread as an ancestor's: a child's parent's, a target's source's. */
export function inheritedOf(plan: Plan<unknown>): readonly Covering[] {
  return plan.child?.covering ?? plan.covering ?? [];
}

/** How a thread started by another links to it: a subagent, or a handoff target. */
function linkOf(plan: Plan<unknown>): ChildPin | undefined {
  const { child, target } = plan;
  if (child !== undefined)
    return {
      parent: { ...child.parent, relation: "subagent" },
      tools: child.tools,
    };
  return target === undefined ? undefined : { parent: target.parent };
}

/**
 * The inputs still to append. A linked thread's inputs are its whole history, so those its log
 * has are skipped; a handed-off thread takes none (rule 26), its run only makes sure the target runs.
 */
function pendingInputs(
  writer: Writer,
  link: ChildPin | undefined,
  inputs: readonly EventDraft[],
): readonly EventDraft[] {
  if (writer.chain.fold.handedOff) return [];
  if (link === undefined) return inputs;
  const given = knownEvents(writer.chain).filter(
    (e) => e.type === "user_input",
  ).length;
  return inputs.slice(given);
}

function handleOf(
  thread: RunOptions<unknown>["thread"],
): ThreadRef | undefined {
  return typeof thread === "object" ? thread : undefined;
}

/** A pin never changes in place: continuing a thread needs the config it started with. */
function checkPin(
  writer: Writer,
  started: ReturnType<typeof pin>["started"],
): void {
  const first = knownEvents(writer.chain).find(
    (e) => e.type === "thread_started",
  );
  // Checked before anything attaches to the thread's sandbox resource.
  if (
    first?.type === "thread_started" &&
    started.type === "thread_started" &&
    first.data.sandbox_provider !== started.data.sandbox_provider
  )
    throw new ConfigError(
      "invalid_config",
      `this thread runs on sandbox provider ${first.data.sandbox_provider ?? "(none)"}, not ${started.data.sandbox_provider ?? "(none)"}`,
    );
  if (
    first?.type === "thread_started" &&
    started.type === "thread_started" &&
    first.data.config_hash !== started.data.config_hash
  )
    throw new ConfigError(
      "invalid_config",
      "this thread was started with another config; a config change starts a new thread",
    );
}

function failed<Output>(error: LogError, thread: ThreadRef): RunResult<Output> {
  const code =
    error.code === "branch_busy" ? "branch_busy" : "branch_not_runnable";
  return { status: "failed", error: { code, message: error.message }, thread };
}
