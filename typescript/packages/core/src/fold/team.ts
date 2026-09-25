import { type KnownEvent, type MailEnvelope, principalKey } from "../log";
import type { EventOf, Fold, ParkAddress } from "./state";

// Teams (spec/schema/README.md, "Teams"): what one log's events leave for semantic rules 31 and
// 33-45, and which received mail renders and opens a turn. Ids are the wire's: MailId, AskId,
// WaitId and MonitorId strings.

/**
 * A turn's run: its opener's principal and the whole root request (thread and event id). A
 * member's task turn has no root request in its own log (it is in the lead's): undefined, and
 * only its principal is compared.
 */
export type TurnRun = {
  readonly principal: string | undefined;
  readonly root:
    | { readonly thread: string; readonly event: string }
    | undefined;
};

export type TeamFold = {
  /** The log starts with team_opened: it takes only operator-side events (rule 33). */
  teamLog: boolean;
  /** team_opened.lead_thread_id, in a team log. */
  leadThread: string | undefined;
  /** thread_started.parent.relation is team_member (rule 41). */
  member: boolean;
  /** A user_input was appended (rule 41). */
  hadInput: boolean;
  /** member_ended was appended (rule 37). */
  ended: boolean;
  /** A member's log took a tree cancel: it opens no turn again (rule 37). */
  stopped: boolean;
  /** The reason of the last turn_completed (rule 38). */
  lastEnd: EventOf<"turn_completed">["data"]["reason"] | undefined;
  /** The run of the open turn (rule 34). */
  turn: TurnRun | undefined;
  /** The run that spawned each background spawn_agent call: a woken turn's run (rules 32, 34). */
  readonly spawns: Map<string, TurnRun>;
  /** Runs (by runKey) with a turn that ended but end_turn: never woken again (rule 32). */
  readonly endedRuns: Set<string>;
  /** Background calls a thread or tree cancel request followed: never woken (rule 32). */
  readonly barred: Set<string>;
  /** Mail received, refused or taken as a task (rule 31). */
  readonly mailDone: Set<string>;
  /** Asks this log received and has not replied to (rule 35). */
  readonly asksIn: Set<string>;
  /** Asks this log sent and has not closed (rules 36, 40). */
  readonly asksOut: Set<string>;
  /** Received replies: reply mail_id to the ask it answers (rule 36). */
  readonly repliesIn: Map<string, string>;
  /** Waits this log started and has not finished (rules 39, 40). */
  readonly waits: Set<string>;
  /** Monitors this log registered that have neither fired nor been observed (rule 39). */
  readonly monitors: Set<string>;
  /** The settle monitors of this log's waits: their notifications open no turn. */
  readonly settle: Set<string>;
  /** The task monitor of each member_started in this log (rule 40). */
  readonly taskMonitors: Set<string>;
  /** operator_request ids in this log (rule 42). */
  readonly requests: Set<string>;
  /** operator_request event ids in this log (rule 42). */
  readonly requestEvents: Set<string>;
};

export function emptyTeam(): TeamFold {
  return {
    teamLog: false,
    leadThread: undefined,
    member: false,
    hadInput: false,
    ended: false,
    stopped: false,
    lastEnd: undefined,
    turn: undefined,
    spawns: new Map(),
    endedRuns: new Set(),
    barred: new Set(),
    mailDone: new Set(),
    asksIn: new Set(),
    asksOut: new Set(),
    repliesIn: new Map(),
    waits: new Set(),
    monitors: new Set(),
    settle: new Set(),
    taskMonitors: new Set(),
    requests: new Set(),
    requestEvents: new Set(),
  };
}

const NOTICES: ReadonlySet<MailEnvelope["kind"]> = new Set([
  "member_settled",
  "member_ended",
]);

/**
 * The mail kinds that render as a `<message>` line and would open a turn: a message, an ask, a
 * bounce naming no ask, or a task or end monitor's notification (reference: turn_open.py). A
 * reply, an ask's bounce and a wait's notification answer their call; a cancel and a park
 * notice reach no model.
 */
export function mailRenders(
  env: MailEnvelope,
  settle: ReadonlySet<string>,
): boolean {
  if (NOTICES.has(env.kind))
    return env.monitor_id !== undefined && !settle.has(env.monitor_id);
  return (
    env.kind === "message" ||
    env.kind === "ask" ||
    (env.kind === "bounce" && env.ask_id === undefined)
  );
}

/**
 * Whether a received mail opens a turn (spec/schema/README.md, "Which events open a turn"): in a
 * member's log (the lead's included) with no turn open, mail that renders, once no park is left
 * but the one it resolves.
 */
export function mailOpensTurn(fold: Fold, env: MailEnvelope): boolean {
  if (fold.turnOpen || fold.team.teamLog) return false;
  const resolves = (p: ParkAddress): boolean =>
    p.kind === "member" && p.id === env.monitor_id;
  return mailRenders(env, fold.team.settle) && fold.parked.every(resolves);
}

/** The run a mail belongs to: its provenance's principal and root request. */
export function mailRun(env: MailEnvelope): TurnRun {
  const { principal, root_request: root } = env.provenance;
  return {
    principal: principalKey(principal),
    root: { thread: root.thread_id, event: root.event_id },
  };
}

/** One run: the same principal and root request (a task turn's, none, only matches none). */
export function sameRun(a: TurnRun, b: TurnRun): boolean {
  return (
    a.principal === b.principal &&
    a.root?.thread === b.root?.thread &&
    a.root?.event === b.root?.event
  );
}

/** A run as a set key. */
export function runKey(run: TurnRun): string {
  return JSON.stringify([run.principal, run.root?.thread, run.root?.event]);
}

/** Whether mail of `run` shares the open turn's run (rule 34). */
export function joinsTurn(turn: TurnRun | undefined, run: TurnRun): boolean {
  if (turn === undefined || turn.principal !== run.principal) return false;
  return (
    turn.root === undefined ||
    (turn.root.thread === run.root?.thread &&
      turn.root.event === run.root.event)
  );
}

/** `<watcher branch_id>:<registering event_id>:<target name>`. */
export function monitorId(e: KnownEvent, target: string): string {
  return `${e.branch_id}:${e.event_id}:${target}`;
}

/** Advances the team bookkeeping past one event that passed validate_next. */
export function applyTeam(fold: Fold, e: KnownEvent): void {
  applyLog(fold.team, e);
  applyTurn(fold, e);
  applyMail(fold, e);
  applyMonitors(fold.team, e);
}

function applyLog(team: TeamFold, e: KnownEvent): void {
  if (e.type === "team_opened") {
    team.teamLog = true;
    team.leadThread = e.data.lead_thread_id;
  } else if (e.type === "thread_started")
    team.member = e.data.parent?.relation === "team_member";
  else if (e.type === "user_input") {
    team.hadInput = true;
    if (e.data.mail_id !== undefined) team.mailDone.add(e.data.mail_id);
  } else if (e.type === "member_ended") team.ended = true;
  else if (e.type === "cancel_requested" && e.data.scope === "tree")
    team.stopped ||= team.member;
  else if (e.type === "operator_request") {
    team.requests.add(e.data.request_id);
    team.requestEvents.add(e.event_id);
  }
}

/** Which run the open turn belongs to. A woken runs before applyWake clears its causes. */
function applyTurn(fold: Fold, e: KnownEvent): void {
  const { team } = fold;
  if (e.type === "user_input") {
    const p = e.actor.principal;
    const task = e.data.source === "team_task";
    team.turn = {
      principal: p === undefined ? undefined : principalKey(p),
      root: task ? undefined : { thread: e.thread_id, event: e.event_id },
    };
  } else if (e.type === "woken") {
    const [first] = e.data.causes;
    const call =
      first === undefined ? undefined : fold.wake.trailingLate.get(first);
    team.turn = call === undefined ? undefined : team.spawns.get(call);
  } else applyRunEnds(team, e);
}

/** Background spawns, and what bars a run's wake (rule 32): a turn that ended otherwise, or a
 * thread or tree cancel request after the spawn. */
function applyRunEnds(team: TeamFold, e: KnownEvent): void {
  if (e.type === "turn_completed") {
    if (e.data.reason !== "end_turn" && team.turn !== undefined)
      team.endedRuns.add(runKey(team.turn));
    team.turn = undefined;
    team.lastEnd = e.data.reason;
  } else if (e.type === "cancel_requested" && e.data.scope !== "turn") {
    for (const call of team.spawns.keys()) team.barred.add(call);
  } else if (
    e.type === "agent_spawned" &&
    e.data.mode === "background" &&
    team.turn !== undefined
  )
    team.spawns.set(e.data.call_id, team.turn);
}

function applyMail(fold: Fold, e: KnownEvent): void {
  const { team } = fold;
  if (e.type === "message_received") received(fold, e.data.envelope);
  else if (e.type === "mail_refused") team.mailDone.add(e.data.mail_id);
  else if (e.type === "ask_closed") team.asksOut.delete(e.data.ask_id);
  else if (e.type === "message_sent") {
    const env = e.data.envelope;
    if (env.kind === "ask" && env.ask_id !== undefined)
      team.asksOut.add(env.ask_id);
    else if (env.kind === "reply" && env.ask_id !== undefined)
      team.asksIn.delete(env.ask_id);
  }
}

/** A turn-opening receipt starts a turn of the mail's run (spec/schema/README.md, "Background
 * wakes"). */
function received(fold: Fold, env: MailEnvelope): void {
  const { team } = fold;
  if (mailOpensTurn(fold, env)) {
    const run = mailRun(env);
    fold.turnOpen = true;
    team.turn = run;
  }
  team.mailDone.add(env.mail_id);
  if (env.kind === "ask" && env.ask_id !== undefined)
    team.asksIn.add(env.ask_id);
  else if (env.kind === "reply" && env.ask_id !== undefined)
    team.repliesIn.set(env.mail_id, env.ask_id);
  else if (NOTICES.has(env.kind) && env.monitor_id !== undefined)
    team.monitors.delete(env.monitor_id);
}

function applyMonitors(team: TeamFold, e: KnownEvent): void {
  if (e.type === "member_started") team.taskMonitors.add(monitorId(e, "task"));
  else if (e.type === "monitor_set")
    team.monitors.add(monitorId(e, e.data.member.name));
  else if (e.type === "wait_started") {
    team.waits.add(e.data.wait_id);
    for (const m of e.data.members) {
      team.monitors.add(monitorId(e, m.name));
      team.settle.add(monitorId(e, m.name));
    }
  } else if (e.type === "wait_finished") team.waits.delete(e.data.wait_id);
  else if (e.type === "member_observed")
    team.monitors.delete(e.data.monitor_id);
}
