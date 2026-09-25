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
import { block, type Define, KEPT_TOOLS } from "./dynamic";
import { sameJson } from "./json";

// Semantic rules 43 and 46 (spec/schema/README.md, "Semantic rules"): what only a team's logs
// together show, checked by the `team` runner and every index rebuild, never by one log's
// validate_next.

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
  /** Each team's log, as its lead's thread_started names it. */
  readonly teamLogs: ReadonlyMap<string, TeamLogRef>;
  /** Each dynamic member thread's define and its starter's name (rule 46). */
  readonly defined: ReadonlyMap<string, Defined>;
};

type Defined = { readonly define: Define; readonly starter: string };

type TeamLogRef = { readonly thread: string; readonly branch: string };

const TEAM_LOG = "team_log";
const member = (team: TeamId, name: string, generation: number): string =>
  `${team}/${name}/${generation}`;
// A caller (Phase 2) speaks for no log of a Phase 1 team: its mail is refused before this rule.
const caller = (c: { readonly branch_id: string }): string =>
  `caller/${c.branch_id}`;

function facts(logs: readonly TeamLogEvents[]): Facts {
  const known = {
    sent: new Map<string, MailEnvelope>(),
    parents: new Map<string, Parent>(),
    identities: new Map<string, Set<string>>(
      logs.map((log) => [log.threadId, new Set<string>()]),
    ),
    teamLogs: new Map<string, TeamLogRef>(),
    defined: new Map<string, Defined>(),
  };
  for (const log of logs) {
    // The starter a block names: operator in a team log, else the log's own agent.
    const first = log.events[0];
    const starter =
      first?.type === "thread_started" ? first.data.agent_name : "operator";
    for (const e of log.events) learn(known, log, e, starter);
  }
  return known;
}

function learn(
  known: {
    readonly sent: Map<string, MailEnvelope>;
    readonly parents: Map<string, Parent>;
    readonly identities: Map<string, Set<string>>;
    readonly teamLogs: Map<string, TeamLogRef>;
    readonly defined: Map<string, Defined>;
  },
  log: TeamLogEvents,
  e: KnownEvent,
  starter: string,
): void {
  const own = known.identities.get(log.threadId);
  if (e.type === "message_sent")
    known.sent.set(e.data.envelope.mail_id, e.data.envelope);
  else if (e.type === "team_opened") own?.add(TEAM_LOG);
  else if (e.type === "thread_started" && e.data.team !== undefined) {
    const { id, log_thread_id, log_branch_id } = e.data.team;
    own?.add(member(id, e.data.agent_name, 1));
    known.teamLogs.set(id, { thread: log_thread_id, branch: log_branch_id });
  } else if (e.type === "member_started") {
    const m = e.data.member;
    known.parents.set(e.data.thread_id, e.data.parent);
    if (e.data.define !== undefined)
      known.defined.set(e.data.thread_id, { define: e.data.define, starter });
    known.identities
      .get(e.data.thread_id)
      ?.add(member(m.team, m.name, m.generation));
  }
}

/**
 * The first break of rule 43, scanning `logs` in order and each log by seq. With `team`, the
 * clauses about one mail check only that team's mail: a nested lead's log also holds its parent
 * team's mail, whose other ends are not among one team's logs. The task-turn clause checks all.
 */
export function checkTeamLogs(
  logs: readonly TeamLogEvents[],
  team?: TeamId,
): CrossFailure | undefined {
  const known = facts(logs);
  for (const log of logs) {
    const found = firstBreak(log, known, team) ?? taskTurn(log, known);
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
      return {
        branchId: log.branchId,
        seq: e.seq,
        message: why.startsWith("46: ") ? why : `43: ${why}`,
      };
  }
  return undefined;
}

/**
 * Mail that renders inside a member's task turn belongs to the task's run: one log shows only
 * the task turn's principal (rule 34), and the task envelope, in the starter's log, holds its
 * root request. Never limited to one team's mail: a nested lead's own-team mail can arrive in
 * the task turn its outer team gave it, and only the outer team's rebuild knows that task.
 */
function taskTurn(log: TeamLogEvents, known: Facts): CrossFailure | undefined {
  const fold = emptyFold();
  let root: MailEnvelope["provenance"]["root_request"] | undefined;
  for (const e of log.events) {
    const inTask =
      fold.team.turn !== undefined && fold.team.turn.root === undefined;
    if (
      e.type === "message_received" &&
      inTask &&
      root !== undefined &&
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
  if (e.type === "team_opened") return named(e, log, known);
  if (e.type === "thread_started") return memberPin(e, log, known);
  if (e.type === "user_input" && e.data.mail_id !== undefined) {
    const task = known.sent.get(e.data.mail_id);
    return task !== undefined &&
      !sameJson(e.actor.principal ?? null, task.provenance.principal)
      ? "a task's input principal is not its mail's provenance principal"
      : undefined;
  }
  return undefined;
}

/** A member's thread_started: its member_started's parent and, for a dynamic member, define. */
function memberPin(
  e: EventOf<"thread_started">,
  log: TeamLogEvents,
  known: Facts,
): string | undefined {
  const parent = known.parents.get(log.threadId);
  if (parent !== undefined && !sameJson(e.data.parent ?? null, parent))
    return "a member's parent is not its member_started's";
  const defined = known.defined.get(log.threadId);
  return defined === undefined ? undefined : pinnedAsDefined(e, defined);
}

/**
 * Rule 46 across logs: a dynamic member pins exactly define.tools and F, and its line 0 ends with
 * the block for define.instructions.
 */
function pinnedAsDefined(
  e: EventOf<"thread_started">,
  { define, starter }: Defined,
): string | undefined {
  const names = e.data.tools
    .map((t) => t.name)
    .filter((n) => !KEPT_TOOLS.has(n));
  if (!sameJson(names, define.tools))
    return "46: a dynamic member's pinned tools are not define.tools and F";
  const written = define.instructions;
  return written === undefined ||
    e.data.instructions.endsWith(block(starter, written))
    ? undefined
    : "46: a dynamic member's instructions don't end with its define's block";
}

/** A team log is the thread and branch its lead's thread_started.team names. */
function named(
  e: EventOf<"team_opened">,
  log: TeamLogEvents,
  known: Facts,
): string | undefined {
  const ref = known.teamLogs.get(e.data.team);
  if (ref === undefined) return undefined;
  return ref.thread === log.threadId && ref.branch === log.branchId
    ? undefined
    : "a team log is not the thread its lead names";
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
      : "caller" in env.to
        ? caller(env.to.caller)
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
      : "caller" in env.from
        ? caller(env.from.caller)
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
  if (!sameJson(env.provenance, refused.provenance))
    return "a bounce's provenance is not its refused mail's";
  if (refused.kind !== "ask")
    return env.ask_id === undefined
      ? undefined
      : "only an ask's bounce names an ask";
  return env.ask_id === refused.ask_id && env.result !== undefined
    ? undefined
    : "an ask's bounce must name the ask and carry the result";
}
