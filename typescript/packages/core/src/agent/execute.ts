import { loopParked } from "../fold/state";
import { ObserverPump } from "../hooks/observers";
import { type LoopConfig, type LoopEnd, resume } from "../loop";
import { knownEvents } from "../reduce";
import { type EventDraft, keepLease, type Writer } from "../store";
import { type Thread, threadHandle } from "../thread/handle";
import { bindBuiltins } from "../tools";
import type { LogError } from "../verify";
import { loopConfig } from "./config";
import { inheritDefer, storeSpecs } from "./defer";
import { ConfigError } from "./errors";
import { observerOf } from "./extension";
import { handedOff } from "./handoff";
import { open } from "./open-thread";
import { type ChildPin, pin } from "./pin";
import { pinChange } from "./pin-change";
import { bindProviders } from "./providers";
import { type RunResult, runResult } from "./result";
import {
  asks,
  ceilingsOf,
  type Hooks,
  inheritedOf,
  type Plan,
  type Resolved,
  type SetUp,
} from "./run";
import { connectAll } from "./setup";
import { skillTools } from "./skills";
import { snapshotTurn } from "./snapshot";
import { openStore } from "./sqlite";
import { leadStarted, teamOf } from "./team/runtime";

// Executing one branch of an agent: its lease (recovery first), its inputs, and the loop until
// the branch is idle or parked; a lead's also runs its team's worker in-process.

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
  await def.setup();
  // This run's own MCP connections, closed when it ends however it ends.
  await using mcp = await connectAll(def.servers);
  const opened = await openStore(store);
  const { log, artifacts } = opened;
  const link = linkOf(plan);
  const context = inheritDefer(def.context, parentDefer(plan));
  const set: SetUp<Deps, Output> = { ...def, context, mcp: mcp.tools };
  const created = pin(set, link, undefined, plan.answerer === true);
  // Durable before any event names them (a thread_started of this pin).
  storeSpecs(artifacts, created.artifacts);
  // Each run is its own executor: a second run on a busy branch is branch_busy.
  const holder = plan.holder ?? `run-${crypto.randomUUID()}`;
  const began = open(
    log,
    child?.threadId ?? target?.threadId ?? plan.thread,
    [leadStarted(def, created.started, log.now()), ...(target?.prefix ?? [])],
    holder,
    link !== undefined,
  );
  const thread = threadHandle(
    opened,
    { id: began.threadId, branch: began.branchId, store },
    def.sandbox === undefined ? {} : { sandbox: def.sandbox },
  );
  if (!began.writer.ok) return failed(began.writer.error, thread);
  const writer = began.writer.value;
  const stop = keepLease(writer);
  const team = teamOf(def, plan, writer, opened);
  try {
    const pinned = asPinned(plan, writer, created, () =>
      pin(set, link, undefined, true),
    );
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
      ...scopeOf(plan),
      ledger: log.budgets,
      inherited: inheritedOf(plan),
      ...(builtin.readFile === undefined ? {} : { readFile: builtin.readFile }),
      ...(child === undefined ? {} : { child }),
      ...(team === undefined ? {} : { team: team.runtime }),
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
        providers.revision,
      );
    if (end.kind === "idle" && writer.chain.fold.handedOff)
      return handedOff(def, plan, writer, thread, config.authorize);
    return runResult(
      end,
      knownEvents(writer.chain),
      loopParked(writer.chain.fold),
      thread,
      def.decode,
    );
  } finally {
    await team?.stop();
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

/** The resolved defer_tools of the thread that started this one, which it inherits. */
function parentDefer(plan: Plan<unknown>): Plan<unknown>["deferTools"] {
  return plan.deferTools ?? plan.child?.deferTools;
}

/** How a thread started by another links to it: a subagent, a handoff target, a member. */
function linkOf(plan: Plan<unknown>): ChildPin | undefined {
  const { child, target, member } = plan;
  if (child !== undefined)
    return {
      parent: { ...child.parent, relation: "subagent" },
      tools: child.tools,
    };
  if (member !== undefined) return { parent: member.parent };
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
      pinChange(first.data, started.data),
    );
}

function failed<Output>(error: LogError, thread: Thread): RunResult<Output> {
  const code =
    error.code === "branch_busy" ? "branch_busy" : "branch_not_runnable";
  return { status: "failed", error: { code, message: error.message }, thread };
}

/** A host continues a thread as it was started: with ask_user when its pin has it. */
function asPinned<P>(
  plan: Plan<unknown>,
  writer: Writer,
  created: P,
  answering: () => P,
): P {
  return plan.answerer === "pinned" && asks(knownEvents(writer.chain))
    ? answering()
    : created;
}

/** What a run inherits from the run that started it: its decision chain and a live eval's stubs. */
function scopeOf(plan: Plan<unknown>): Pick<Plan<unknown>, "chain" | "stub"> {
  return {
    ...(plan.chain === undefined ? {} : { chain: plan.chain }),
    ...(plan.stub === undefined ? {} : { stub: plan.stub }),
  };
}
