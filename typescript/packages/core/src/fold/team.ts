import { type KnownEvent, type MailEnvelope, principalKey } from "../log";
import type { EventOf, Fold, ParkAddress } from "./state";

// Teams (spec/schema/README.md, "Teams"): what one log's events leave for semantic rules 31 and
// 33-45, and which received mail opens a turn. Ids are the wire's: MailId, AskId, WaitId and
// MonitorId strings.

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
  /** The reason of the last turn_completed (rule 38). */
  lastEnd: EventOf<"turn_completed">["data"]["reason"] | undefined;
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
};

export function emptyTeam(): TeamFold {
  return {
    teamLog: false,
    leadThread: undefined,
    member: false,
    hadInput: false,
    ended: false,
    lastEnd: undefined,
    mailDone: new Set(),
    asksIn: new Set(),
    asksOut: new Set(),
    repliesIn: new Map(),
    waits: new Set(),
    monitors: new Set(),
    settle: new Set(),
    taskMonitors: new Set(),
    requests: new Set(),
  };
}

const NOTICES: ReadonlySet<MailEnvelope["kind"]> = new Set([
  "member_settled",
  "member_ended",
]);

/**
 * Whether a received mail opens a turn (spec/schema/README.md, "Which events open a turn";
 * reference: turn_open.py). Only in a member's log (the lead's included) with no turn open: a
 * message, an ask, a bounce naming no ask, or a task or end monitor's notification, once no park
 * is left but the one it resolves. A reply, an ask's bounce and a wait's notification resume
 * their call instead.
 */
export function mailOpensTurn(fold: Fold, env: MailEnvelope): boolean {
  if (fold.turnOpen || fold.team.teamLog) return false;
  const opens = NOTICES.has(env.kind)
    ? env.monitor_id !== undefined && !fold.team.settle.has(env.monitor_id)
    : env.kind === "message" ||
      env.kind === "ask" ||
      (env.kind === "bounce" && env.ask_id === undefined);
  const resolves = (p: ParkAddress): boolean =>
    p.kind === "member" && p.id === env.monitor_id;
  return opens && fold.parked.every(resolves);
}

/** `<watcher branch_id>:<registering event_id>:<target name>`. */
export function monitorId(e: KnownEvent, target: string): string {
  return `${e.branch_id}:${e.event_id}:${target}`;
}

/** Advances the team bookkeeping past one event that passed validate_next. */
export function applyTeam(fold: Fold, e: KnownEvent): void {
  applyLog(fold.team, e);
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
    if (e.data.source === "team_task") team.mailDone.add(e.data.mail_id);
  } else if (e.type === "turn_completed") team.lastEnd = e.data.reason;
  else if (e.type === "member_ended") team.ended = true;
  else if (e.type === "operator_request") team.requests.add(e.data.request_id);
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

/** A turn-opening receipt starts a turn of the mail's run: its provenance's principal and root
 * request (spec/schema/README.md, "Background wakes"). */
function received(fold: Fold, env: MailEnvelope): void {
  const { team } = fold;
  if (mailOpensTurn(fold, env)) {
    fold.turnOpen = true;
    fold.wake.run = {
      principal: principalKey(env.provenance.principal),
      root: env.provenance.root_request.event_id,
    };
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
