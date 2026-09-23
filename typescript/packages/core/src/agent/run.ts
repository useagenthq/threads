import {
  BranchId,
  type InputPart,
  type KnownEvent,
  type Principal,
  ThreadId,
} from "../log";
import { type LoopConfig, type LoopEnd, resume, type ToolImpl } from "../loop";
import { toolSpec } from "../loop/turn";
import type { Model } from "../model";
import { category, decide } from "../permissions";
import { knownEvents } from "../reduce";
import { LEASE_TTL_MS, type LogStore, type Writer } from "../store";
import { uuidv7 } from "../store/encode";
import { bindBuiltins } from "../tools";
import type { LogError } from "../verify";
import { ConfigError } from "./errors";
import type { PinOptions } from "./pin";
import { pin } from "./pin";
import {
  type Decode,
  type RunResult,
  runResult,
  type ThreadRef,
} from "./result";
import { redactSecrets } from "./secret";
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
  readonly decode: Decode<Output>;
};

/** The local operator. */
const OPERATOR: Principal = {
  issuer: "api",
  tenant: "local",
  subject: "operator",
};
const WORKSPACE = "/workspace";

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
  const store =
    options.store ?? handleOf(options.thread)?.store ?? sqlite(".threads");
  const { log, artifacts } = await openStore(store);
  const pinned = pin(def);
  // Each run is its own executor: a second run on a busy branch is branch_busy.
  const holder = `run-${crypto.randomUUID()}`;
  const opened = open(log, options.thread, pinned.started, holder);
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
    const principal = options.principal ?? OPERATOR;
    const builtin = bindBuiltins(def.sandbox, def.egress, {
      ledger: log.ledger,
      writer,
      artifacts,
    });
    const config = loopConfig(
      def,
      options,
      principal,
      thread,
      hooks,
      builtin.tools,
    );
    const result = (end: LoopEnd): RunResult<Output> =>
      runResult(
        end,
        knownEvents(writer.chain),
        writer.chain.fold.parked,
        thread,
        def.decode,
      );
    // Recovery first; an in-doubt turn finishes before the new input opens the next one.
    const end = await resume(writer, artifacts, config, {
      input: {
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
      },
    });
    if (end.kind === "idle")
      await snapshotTurn(def.sandbox, builtin.session, log.ledger, writer);
    return result(end);
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

/** A new thread (its header and thread_started), or the lease on an existing one's branch. */
function open(
  log: LogStore,
  thread: RunOptions<unknown>["thread"],
  started: ReturnType<typeof pin>["started"],
  holder: string,
): Opened {
  if (thread === undefined) {
    const threadId = ThreadId.parse(uuidv7(Date.now()));
    const branchId = BranchId.parse(uuidv7(Date.now()));
    const created = log.createBranch(threadId, branchId);
    if (!created.ok) return { threadId, branchId, writer: created };
    const writer = log.acquire(branchId, holder);
    if (writer.ok) {
      const first = writer.value.append([started]);
      if (!first.ok) return { threadId, branchId, writer: first };
    }
    return { threadId, branchId, writer };
  }
  const threadId = typeof thread === "string" ? thread : thread.id;
  const branch =
    typeof thread === "string"
      ? log.mainBranch(thread)
      : { ok: true as const, value: thread.branch };
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

/** A pin never changes in place: continuing a thread needs the config it started with. */
function checkPin(
  writer: Writer,
  started: ReturnType<typeof pin>["started"],
): void {
  const first = knownEvents(writer.chain).find(
    (e) => e.type === "thread_started",
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

function loopConfig<Deps, Output>(
  def: Resolved<Deps, Output>,
  options: RunOptions<Deps>,
  principal: Principal,
  thread: ThreadRef,
  hooks: Hooks,
  builtin: readonly ToolImpl[],
): LoopConfig {
  const env = {
    deps: options.deps,
    threadId: thread.id,
    branchId: thread.branch,
    principal,
  };
  const models: readonly Model[] = [def.model, ...def.fallback];
  return {
    models: (ref) =>
      models.find(
        (m) =>
          m.info.model.provider === ref.provider &&
          m.info.model.name === ref.name,
      ),
    tools: new Map([
      ...builtin.map((t) => [t.spec.name, t] as const),
      ...def.bindable.map((t) => [t.name, t.bind(env)] as const),
    ]),
    authorize: (call, fold) => {
      const permissions = fold.policy?.permissions;
      if (permissions === undefined)
        return { decision: "ask", source: "default" };
      const spec = toolSpec(fold, call.data.name);
      const d = decide(permissions, WORKSPACE, {
        tool: call.data.name,
        category: category(call.data.name, spec?.effect_class),
        input: call.data.input,
        mode: fold.mode,
      });
      return {
        decision: d.decision,
        source: d.source,
        ...(d.rule === undefined ? {} : { rule_id: d.rule }),
      };
    },
    clock: {
      now: Date.now,
      sleepUntil: async (time) => {
        const { promise, resolve } = Promise.withResolvers<void>();
        setTimeout(resolve, Math.max(0, time - Date.now()));
        await promise;
      },
    },
    principal,
    skewMarginMs: 1000,
    redact: redactSecrets,
    ...(def.output === undefined ? {} : { output: def.output }),
    ...(options.signal === undefined ? {} : { signal: options.signal }),
    ...(hooks.onEvent === undefined ? {} : { onEvent: hooks.onEvent }),
    ...(hooks.onDelta === undefined ? {} : { onDelta: hooks.onDelta }),
  };
}
