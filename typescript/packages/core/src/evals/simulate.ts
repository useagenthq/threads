import type { Agent } from "../agent/agent";
import { type LiveRun, liveRunOf } from "../agent/registry";
import type { RunResult, ThreadRef } from "../agent/result";
import { canonicalize } from "../log";
import type { CaseDir } from "./case-dir";
import type { CaseLog } from "./checks";
import type { CaseSimulate } from "./files";
import { JUDGE_CONVERSATION_V1, judgeItems } from "./judge";
import { runJudge } from "./judge-run";
import { handleFor, type Ledger, spentSoFar, startLedger } from "./ledger";
import {
  answerOf,
  costOf,
  type Env,
  EVAL_PRINCIPAL,
  eventsOf,
  guarded,
  type LiveOutcome,
  NO_CALLS,
  unfinished,
} from "./live-env";
import { remainingBudget } from "./remaining-budget";
import type { Simulation } from "./schema";
import { type Plan, planPrefix, prefixTexts } from "./simulate-prefix";
import { type VisibleMessage, visibleConversation } from "./simulated-user";
import { playerFor, type UserPlayer } from "./simulated-user-run";
import { type StubQueue, stubQueue } from "./stub-queue";
import { treeTotals } from "./tree-totals";

// A simulated case's live conversation (spec lane 32, B): the real turns before the saved one
// are the prefix -- continued when the agent is unchanged, re-driven when it is not -- and the
// saved turn's text is the opener the simulation starts from. Every effectful call answers from
// the recorded stubs on every turn, and one budget covers the whole conversation.

const DEFAULT_MAX_MESSAGES = 5;

const skipped = (reason: string): LiveOutcome => ({
  kind: "skipped",
  reason,
  calls: NO_CALLS,
  cost: null,
});

type Loop = {
  readonly run: LiveRun;
  readonly plan: Plan;
  readonly player: UserPlayer;
  readonly stub: StubQueue;
  readonly max: number;
  readonly env: Env;
};

type Ended =
  | { readonly kind: "ok"; readonly ending: Simulation["ended"] }
  | { readonly kind: "error"; readonly reason: string }
  | { readonly kind: "blocked"; readonly model: string };

type Running = {
  thread: ThreadRef | undefined;
  lead: Extract<RunResult<unknown>, { status: "completed" }> | undefined;
  messages: number;
  readonly ledger: Ledger;
};

/** The agent's reply as the user and the judge see it: its text, or its canonical JSON. */
function replyText(output: unknown): string {
  if (typeof output === "string") return output;
  const text = canonicalize(answerOf(output));
  if (!text.ok) throw new Error("a run output is canonical JSON");
  return text.value;
}

/** What the conversation may still spend, or the error that ends it. */
async function left(l: Loop, state: Running) {
  const spent = await spentSoFar(
    l.env,
    state.ledger,
    state.thread,
    l.player.thread(),
  );
  return remainingBudget(l.env.live.budget, spent);
}

/** One agent run under what is left of the conversation budget. */
async function sendTurn(
  l: Loop,
  state: Running,
  text: string,
): Promise<Ended | undefined> {
  const budget = await left(l, state);
  if (!budget.ok) return { kind: "error", reason: "budget_exhausted" };
  const ran = await guarded(() =>
    l.run(text, {
      store: l.env.store,
      principal: EVAL_PRINCIPAL,
      budget: budget.budget,
      stub: l.stub,
      ...(state.thread === undefined ? {} : { thread: state.thread }),
    }),
  );
  if ("blocked" in ran) return { kind: "blocked", model: ran.blocked };
  if ("config" in ran) return { kind: "error", reason: ran.config };
  state.thread = ran.thread;
  const missed = l.stub.firstUnmatched();
  if (missed !== undefined)
    return { kind: "error", reason: `unmatched_external_op: ${missed}` };
  if (ran.status !== "completed")
    return { kind: "error", reason: unfinished(ran) };
  state.lead = ran;
  return undefined;
}

type Round =
  | { readonly kind: "seen"; readonly seen: readonly VisibleMessage[] }
  | Ended;

/** One round: what the user says next, then the agent's answer to it. */
async function round(
  l: Loop,
  state: Running,
  seen: readonly VisibleMessage[],
): Promise<Round> {
  const budget = await left(l, state);
  if (!budget.ok) return { kind: "error", reason: "budget_exhausted" };
  const play = await l.player.next(seen, budget.budget);
  if (play.kind !== "message")
    return play.kind === "ended" ? { kind: "ok", ending: play.ending } : play;
  if (state.messages >= l.max) return { kind: "ok", ending: "max_messages" };
  state.messages += 1;
  const stopped = await sendTurn(l, state, play.text);
  return (
    stopped ?? {
      kind: "seen",
      seen: [{ from: "agent", text: replyText(state.lead?.output) }],
    }
  );
}

/** B.2: re-drive the prefix, send the opener, then alternate user and agent until someone stops. */
async function converse(
  l: Loop,
  opener: string,
  state: Running,
): Promise<Ended> {
  for (const text of l.plan.redrive) {
    const stopped = await sendTurn(l, state, text);
    if (stopped !== undefined) return stopped;
  }
  const opened = await sendTurn(l, state, opener);
  if (opened !== undefined) return opened;
  // The simulator's first input is the whole visible conversation, read off the log.
  let seen: readonly VisibleMessage[] =
    state.thread === undefined
      ? []
      : visibleConversation(await eventsOf(l.env.store, state.thread));
  for (;;) {
    const next = await round(l, state, seen);
    if (next.kind !== "seen") return next;
    seen = next.seen;
  }
}

const maxMessages = (simulate: CaseSimulate): number =>
  simulate.kind === "model"
    ? (simulate.max_messages ?? DEFAULT_MAX_MESSAGES)
    : simulate.messages.length + 1;

/** The conversation's cost and model calls, whatever ended it. */
async function totals(l: Loop, state: Running) {
  const user = l.player.thread();
  const threads = [
    ...(state.thread === undefined
      ? []
      : [await handleFor(l.env, state.thread)]),
    ...(user === undefined ? [] : [user]),
  ];
  const agent =
    state.thread === undefined
      ? 0
      : (await treeTotals(l.env.store, state.thread, l.plan.since)).requests;
  return {
    calls: { agent, user: await l.player.calls(), judge: 0 },
    cost: await costOf(threads),
    user,
  };
}

function simulationOf(
  l: Loop,
  state: Running,
  ending: Simulation["ended"],
  userThread: { readonly id: string } | undefined,
): Simulation {
  return {
    messages: state.messages,
    prefix: l.plan.mode,
    prefix_turns: l.plan.turns,
    ended: ending,
    ...(l.env.kept && userThread !== undefined
      ? { user_thread_id: userThread.id }
      : {}),
  };
}

/** Grades the whole conversation: the opener is the task, the rest is the transcript. */
async function grade(
  l: Loop,
  state: Running,
  opener: string,
  criteria: readonly string[],
  simulate: CaseSimulate,
  simulation: Simulation,
  spent: Awaited<ReturnType<typeof totals>>,
): Promise<LiveOutcome> {
  const thread = state.thread;
  if (thread === undefined)
    return {
      kind: "error",
      reason: "no_run",
      calls: spent.calls,
      cost: spent.cost,
    };
  const ask = {
    instructions: JUDGE_CONVERSATION_V1,
    task: opener,
    events: await eventsOf(l.env.store, thread),
    answer: answerOf(state.lead?.output),
    rubric: criteria,
    prefixTurns: l.plan.turns,
    ...(simulate.kind === "model" ? { goal: simulate.goal } : {}),
  };
  const { answer } = ask;
  const items = judgeItems(ask);
  const judged = await runJudge(ask, l.env);
  if (judged.kind === "blocked")
    return { kind: "blocked", model: judged.model };
  const calls = { ...spent.calls, judge: judged.calls };
  const cost = await costOf([
    await handleFor(l.env, thread),
    ...(spent.user === undefined ? [] : [spent.user]),
    ...(judged.thread === undefined ? [] : [judged.thread]),
  ]);
  if (judged.kind === "error")
    return { kind: "error", reason: judged.reason, calls, cost, simulation };
  const passed = judged.graded.filter((v) => v.pass).length;
  return {
    kind: "graded",
    check: {
      answer,
      transcript_items: items.length,
      score: passed / judged.graded.length,
      fresh_sandbox: l.env.fresh,
      verdicts: [...judged.graded],
      ...(l.env.kept
        ? { thread_id: thread.id, judge_thread_id: judged.thread.id }
        : {}),
    },
    calls,
    cost,
    simulation,
  };
}

/** Runs a simulated case's conversation and grades it (spec lane 32, B and D). */
export async function simulateCase(
  c: CaseDir,
  log: CaseLog,
  target: Agent<never, unknown>,
  criteria: readonly string[],
  simulate: CaseSimulate,
  env: Env,
): Promise<LiveOutcome> {
  if (c.meta.simulate_blocked !== undefined)
    return skipped(c.meta.simulate_blocked);
  const opener = c.meta.input?.text;
  const run = liveRunOf(target);
  if (opener === undefined || prefixTexts(log).includes(undefined))
    return skipped("simulate_content_input");
  if (run === undefined) return skipped("offline_not_runnable:content_input");
  const player = playerFor(simulate, env);
  if ("blocked" in player) return { kind: "blocked", model: player.blocked };
  const plan = await planPrefix(c, log, target, env);
  const l: Loop = {
    run,
    plan,
    player,
    stub: stubQueue(
      c.stubs ?? { stubs: [] },
      plan.mode === "redriven" ? "conversation" : "turn",
    ),
    max: maxMessages(simulate),
    env,
  };
  const state: Running = {
    thread: plan.thread,
    lead: undefined,
    messages: 1,
    ledger: startLedger(plan.since, plan.baseCostNanos),
  };
  const end = await converse(l, opener, state);
  if (end.kind === "blocked") return { kind: "blocked", model: end.model };
  const spent = await totals(l, state);
  if (end.kind === "error")
    return {
      kind: "error",
      reason: end.reason,
      calls: spent.calls,
      cost: spent.cost,
    };
  const simulation = simulationOf(l, state, end.ending, spent.user);
  return grade(l, state, opener, criteria, simulate, simulation, spent);
}
