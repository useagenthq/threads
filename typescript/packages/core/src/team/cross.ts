import { apply } from "../fold/apply";
import { type EventOf, emptyFold } from "../fold/state";
import { mailRenders } from "../fold/team";
import type {
  BranchId,
  KnownEvent,
  MailEnvelope,
  TeamId,
  ThreadId,
} from "../log";
import { sameJson } from "./json";

// Semantic rule 43 (spec/schema/README.md, "Semantic rules"): what only a team's logs together
// show, checked by the `team` runner and every index rebuild, never by one log's validate_next.

/** One of a team's logs, read and verified. */
export type TeamLogEvents = {
  readonly threadId: ThreadId;
  readonly branchId: BranchId;
  readonly events: readonly KnownEvent[];
};

/** The first event that breaks rule 43. */
export type CrossFailure = {
  readonly branchId: BranchId;
  readonly seq: number;
  readonly message: string;
};

type Parent = EventOf<"member_started">["data"]["parent"];

/** What the rule reads across the logs. */
type Facts = {
  /** Every message_sent envelope, by mail id. */
  readonly sent: ReadonlyMap<string, MailEnvelope>;
  /** Each member thread's parent, as its member_started names it. */
  readonly parents: ReadonlyMap<string, Parent>;
  /** Who each log speaks for: `team_log`, or `<team>/<name>/<generation>`. */
  readonly identities: ReadonlyMap<string, ReadonlySet<string>>;
};

const TEAM_LOG = "team_log";
const member = (team: TeamId, name: string, generation: number): string =>
  `${team}/${name}/${generation}`;

function facts(logs: readonly TeamLogEvents[]): Facts {
  const known = {
    sent: new Map<string, MailEnvelope>(),
    parents: new Map<string, Parent>(),
    identities: new Map<string, Set<string>>(
      logs.map((log) => [log.threadId, new Set<string>()]),
    ),
  };
  for (const log of logs) for (const e of log.events) learn(known, log, e);
  return known;
}

function learn(
  known: {
    readonly sent: Map<string, MailEnvelope>;
    readonly parents: Map<string, Parent>;
    readonly identities: Map<string, Set<string>>;
  },
  log: TeamLogEvents,
  e: KnownEvent,
): void {
  const own = known.identities.get(log.threadId);
  if (e.type === "message_sent")
    known.sent.set(e.data.envelope.mail_id, e.data.envelope);
  else if (e.type === "team_opened") own?.add(TEAM_LOG);
  else if (e.type === "thread_started" && e.data.team !== undefined)
    own?.add(member(e.data.team.id, e.data.agent_name, 1));
  else if (e.type === "member_started") {
    const m = e.data.member;
    known.parents.set(e.data.thread_id, e.data.parent);
    known.identities
      .get(e.data.thread_id)
      ?.add(member(m.team, m.name, m.generation));
  }
}

/**
 * The first break of rule 43, scanning `logs` in order and each log by seq. With `team`, only
 * that team's mail is checked: a nested lead's log also holds its parent team's mail, whose
 * other ends are not among one team's logs.
 */
export function checkTeamLogs(
  logs: readonly TeamLogEvents[],
  team?: TeamId,
): CrossFailure | undefined {
  const known = facts(logs);
  for (const log of logs) {
    const found = firstBreak(log, known, team) ?? taskTurn(log, known, team);
    if (found !== undefined) return found;
  }
  return undefined;
}

/** Only `team`'s mail, when a team is given. */
function outside(e: KnownEvent, team: TeamId | undefined): boolean {
  const mail =
    e.type === "message_sent" || e.type === "message_received"
      ? e.data.envelope.team
      : undefined;
  return team !== undefined && mail !== undefined && mail !== team;
}

function firstBreak(
  log: TeamLogEvents,
  known: Facts,
  team: TeamId | undefined,
): CrossFailure | undefined {
  for (const e of log.events) {
    const why = outside(e, team) ? undefined : check(e, log, known);
    if (why !== undefined)
      return { branchId: log.branchId, seq: e.seq, message: `43: ${why}` };
  }
  return undefined;
}

/**
 * Mail that renders inside a member's task turn belongs to the task's run: one log shows only
 * the task turn's principal (rule 34), and the task envelope, in the starter's log, holds its
 * root request.
 */
function taskTurn(
  log: TeamLogEvents,
  known: Facts,
  team: TeamId | undefined,
): CrossFailure | undefined {
  const fold = emptyFold();
  let root: MailEnvelope["provenance"]["root_request"] | undefined;
  for (const e of log.events) {
    const inTask =
      fold.team.turn !== undefined && fold.team.turn.root === undefined;
    if (
      e.type === "message_received" &&
      inTask &&
      root !== undefined &&
      !outside(e, team) &&
      mailRenders(e.data.envelope, fold.team.settle) &&
      !sameJson(e.data.envelope.provenance.root_request, root)
    )
      return {
        branchId: log.branchId,
        seq: e.seq,
        message: "43: mail of another run joins a member's task turn",
      };
    if (e.type === "user_input" && e.data.mail_id !== undefined)
      root = known.sent.get(e.data.mail_id)?.provenance.root_request ?? root;
    apply(fold, { kind: "event", event: e });
  }
  return undefined;
}

function check(
  e: KnownEvent,
  log: TeamLogEvents,
  known: Facts,
): string | undefined {
  const own = known.identities.get(log.threadId) ?? new Set<string>();
  if (e.type === "message_received") return received(e, own, known);
  if (e.type === "message_sent") return sent(e, log, own, known);
  if (e.type === "thread_started") {
    const parent = known.parents.get(log.threadId);
    return parent !== undefined && !sameJson(e.data.parent ?? null, parent)
      ? "a member's parent is not its member_started's"
      : undefined;
  }
  if (e.type === "user_input" && e.data.mail_id !== undefined) {
    const task = known.sent.get(e.data.mail_id);
    return task !== undefined &&
      !sameJson(e.actor.principal ?? null, task.provenance.principal)
      ? "a task's input principal is not its mail's provenance principal"
      : undefined;
  }
  return undefined;
}

function received(
  e: EventOf<"message_received">,
  own: ReadonlySet<string>,
  known: Facts,
): string | undefined {
  const env = e.data.envelope;
  const original = known.sent.get(e.data.mail_id);
  if (original !== undefined && !sameJson(original, env))
    return "a receipt differs from its sender's mail";
  const to =
    env.to === TEAM_LOG
      ? TEAM_LOG
      : member(env.team, env.to.name, env.to.generation);
  return own.has(to) ? undefined : "a receipt is not in the log `to` names";
}

function sent(
  e: EventOf<"message_sent">,
  log: TeamLogEvents,
  own: ReadonlySet<string>,
  known: Facts,
): string | undefined {
  const env = e.data.envelope;
  const from =
    "operator" in env.from
      ? TEAM_LOG
      : member(env.from.team, env.from.name, env.from.generation);
  if (!own.has(from)) return "a mail is not in the log `from` names";
  return env.kind === "bounce" ? bounce(env, log, known) : undefined;
}

/** A bounce's causal is its refuser's mail_refused; it names an ask exactly when it refused one. */
function bounce(
  env: MailEnvelope,
  log: TeamLogEvents,
  known: Facts,
): string | undefined {
  const refusal = log.events.find((e) => e.event_id === env.causal.event_id);
  if (refusal?.type !== "mail_refused")
    return "a bounce's causal is not its mail_refused";
  const refused = known.sent.get(refusal.data.mail_id);
  if (refused === undefined) return undefined;
  if (refused.kind !== "ask")
    return env.ask_id === undefined
      ? undefined
      : "only an ask's bounce names an ask";
  return env.ask_id === refused.ask_id && env.result !== undefined
    ? undefined
    : "an ask's bounce must name the ask and carry the result";
}
