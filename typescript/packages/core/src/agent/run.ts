import { ObserverPump } from "../hooks/observers";
import {
  BranchId,
  type InputPart,
  type KnownEvent,
  type Principal,
  ThreadId,
} from "../log";
import { type ChildRun, type LoopEnd, resume } from "../loop";
import { knownEvents } from "../reduce";
import {
  type EventDraft,
  LEASE_TTL_MS,
  type LogStore,
  type Writer,
} from "../store";
import { uuidv7 } from "../store/encode";
import { bindBuiltins } from "../tools";
import type { LogError } from "../verify";
import type { Agent } from "./agent";
import { loopConfig } from "./config";
import { ConfigError } from "./errors";
import { type Extension, observerOf } from "./extension";
import type { PinOptions } from "./pin";
import { pin } from "./pin";
import {
  type Decode,
  type RunResult,
  runResult,
  type ThreadRef,
} from "./result";
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
};

export type Resolved<Deps, Output> = PinOptions & {
  readonly bindable: readonly Tool<unknown, unknown, Deps>[];
  readonly hookable: readonly Extension<Deps>[];
  readonly setup: () => Promise<void>;
  readonly decode: Decode<Output>;
  /** The agents behind PinOptions.subagents, by name. */
  readonly agents: readonly Agent<never, unknown>[];
};

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
  const { store, child, principal } = plan;
  const { log, artifacts } = await openStore(store);
  const pinned = pin(
    def,
    child === undefined
      ? undefined
      : {
          parent: { ...child.parent, relation: "subagent" },
          tools: child.tools,
        },
  );
  await def.setup();
  // Each run is its own executor: a second run on a busy branch is branch_busy.
  const holder = `run-${crypto.randomUUID()}`;
  const target = child?.threadId ?? plan.thread;
  const opened = open(log, target, pinned.started, holder, child !== undefined);
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
    );
    const observers = new ObserverPump(
      log.cursors,
      thread.branch,
      () => knownEvents(writer.chain),
      def.hookable.flatMap((e) => observerOf(e) ?? []),
    );
    observers.poke();
    const config = loopConfig(def, {
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
      builtin: builtin.tools,
      ...(builtin.readFile === undefined ? {} : { readFile: builtin.readFile }),
      ...(child === undefined ? {} : { child }),
    });
    const given = knownEvents(writer.chain).filter(
      (e) => e.type === "user_input",
    ).length;
    const pending = child === undefined ? inputs : inputs.slice(given);
    // Recovery first; an in-doubt turn finishes before the next input opens the next one.
    let end: LoopEnd = await resume(writer, artifacts, config, {
      ...(pending[0] === undefined ? {} : { input: pending[0] }),
    });
    for (const input of pending.slice(1)) {
      if (end.kind !== "idle") break;
      end = await resume(writer, artifacts, config, { input });
    }
    if (end.kind === "idle" && child === undefined)
      await snapshotTurn(def.sandbox, builtin.session, log.ledger, writer);
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

/**
 * Renews the lease every third of its TTL while the run is in flight, so slow model and tool
 * calls keep it, then hands it back so the next run starts at once. A failed renewal
 * poisons the writer, which fences every later dispatch and append.
 */
function keepLease(writer: Writer): () => void {
  const timer = setInterval(() => {
    if (!writer.renew(LEASE_TTL_MS).ok) clearInterval(timer);
  }, LEASE_TTL_MS / 3);
  return () => {
    clearInterval(timer);
    writer.release();
  };
}

function handleOf(
  thread: RunOptions<unknown>["thread"],
): ThreadRef | undefined {
  return typeof thread === "object" ? thread : undefined;
}

type Opened = {
  readonly threadId: ThreadId;
  readonly branchId: BranchId;
  readonly writer: ReturnType<LogStore["acquire"]>;
};

/**
 * A new thread (its header and thread_started), or the lease on an existing one's branch. With
 * `create`, a thread id that isn't in the store yet is created under that id (a child).
 */
function open(
  log: LogStore,
  thread: RunOptions<unknown>["thread"],
  started: ReturnType<typeof pin>["started"],
  holder: string,
  create: boolean,
): Opened {
  if (thread === undefined)
    return created(log, ThreadId.parse(uuidv7(Date.now())), started, holder);
  const threadId = typeof thread === "string" ? thread : thread.id;
  const branch =
    typeof thread === "string"
      ? log.mainBranch(thread)
      : { ok: true as const, value: thread.branch };
  if (!branch.ok && create) return created(log, threadId, started, holder);
  if (!branch.ok)
    throw new ConfigError(
      "invalid_config",
      `thread ${threadId} is not in this store`,
    );
  return {
    threadId,
    branchId: branch.value,
    writer: log.acquire(branch.value, holder),
  };
}

function created(
  log: LogStore,
  threadId: ThreadId,
  started: ReturnType<typeof pin>["started"],
  holder: string,
): Opened {
  const branchId = BranchId.parse(uuidv7(Date.now()));
  const made = log.createBranch(threadId, branchId);
  if (!made.ok) return { threadId, branchId, writer: made };
  const writer = log.acquire(branchId, holder);
  if (writer.ok) {
    const first = writer.value.append([started]);
    if (!first.ok) return { threadId, branchId, writer: first };
  }
  return { threadId, branchId, writer };
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
