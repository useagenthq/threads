import type { HostFold } from "../fold/host";
import { senderClass } from "../fold/host";
import type { EventOf, Fold } from "../fold/state";
import type { KnownEvent, MailEnvelope } from "../log";
import { hostTeamPart } from "../team/host-team";
import { sameJson } from "../team/json";
import { checkRestarted } from "./supervision";
import { invalid, type Violation } from "./violation";

// Semantic rules 50 and 52-55 on one log (spec/schema/README.md, "Teams Phase 2"): host team
// logs, callers, a host member's turn failures and the failed ask. Rule 51's own checks are in
// supervision.ts, which this module's member_started case calls; rule 43's Phase 2 clauses and
// rule 51's cross-log clause are in team/cross.ts. Reference: spec/tools/fixtures/ref_host.py.

/** Rule 50: a host team's log is at the thread, branch and team id its tenant derives. */
function openedAtDerivedIds(e: EventOf<"team_opened">): boolean {
  const tenant = e.data.tenant;
  if (tenant === undefined) return false;
  return (
    e.data.team === hostTeamPart("team", tenant) &&
    e.thread_id === hostTeamPart("log_thread", tenant) &&
    e.branch_id === hostTeamPart("log_branch", tenant)
  );
}

/** Rule 52: host team mail belongs to the host team its provenance principal's tenant derives. */
function tenantTeam(env: MailEnvelope): boolean {
  return env.team === hostTeamPart("team", env.provenance.principal.tenant);
}

/**
 * Rule 50: the host-only events and forms stay in a host team's or a host member's log, and a
 * host team's log is at its tenant's derived ids.
 */
export function checkHostPlacement(fold: Fold, e: KnownEvent): Violation {
  const { host } = fold.team;
  if (e.type === "team_opened" && e.data.kind === "host")
    return openedAtDerivedIds(e)
      ? undefined
      : invalid("a host team's ids are not the ones its tenant derives");
  if (
    e.type === "member_started" &&
    (e.data.host_member !== undefined) !== host.hostTeam
  )
    return invalid(
      "a host team log starts only host members, and only it does",
    );
  if (
    e.type === "member_idle" &&
    e.data.turn_failed !== undefined &&
    !host.hostMember
  )
    return invalid("member_idle{turn_failed} outside a host member's log");
  return e.type === "budget_exceeded" &&
    e.data.scope === "hop" &&
    !host.hostMember
    ? invalid("a hop cap outside a host member's log")
    : undefined;
}

/**
 * Rule 51: in a host team's log, a first host member start is generation 1, once per name, with
 * no provenance; a start with `restart_of` is a supervised or operator restart (supervision.ts).
 */
export function checkHostStarted(
  fold: Fold,
  e: EventOf<"member_started">,
): Violation {
  const { host } = fold.team;
  if (!host.hostTeam) return undefined;
  const restartOf = e.data.restart_of;
  if (restartOf !== undefined) return checkRestarted(fold, e, restartOf);
  const { name, generation } = e.data.member;
  const first =
    generation === 1 &&
    !host.generations.has(name) &&
    e.data.provenance === undefined;
  return first
    ? undefined
    : invalid(
        "a first host member start is generation 1, once, with no provenance",
      );
}

/**
 * Rule 53: once a host member's turn has failed, its append holds only that turn's turn_failed
 * bounces and then member_idle{turn_failed} — or a member_ended, which ends the member instead.
 */
export function checkWhileFailing(fold: Fold, e: KnownEvent): Violation {
  if (!fold.team.host.failing) return undefined;
  const bounce =
    e.type === "message_sent" && e.data.envelope.code === "turn_failed";
  const done =
    e.type === "member_ended" ||
    (e.type === "member_idle" && e.data.turn_failed !== undefined);
  return bounce || done
    ? undefined
    : invalid(
        `${e.type} before a failed turn's bounces and member_idle{turn_failed}`,
      );
}

/**
 * Rules 52 and 53 for mail this log sends: a caller's mail names this log and its tenant's host
 * team, and a turn_failed bounce answers one unanswered ask of the failed turn.
 */
export function checkHostSent(
  fold: Fold,
  e: EventOf<"message_sent">,
): Violation {
  const { host } = fold.team;
  const env = e.data.envelope;
  const from = env.from;
  if ("caller" in from) {
    const why = callerSent(host, e, from.caller);
    if (why !== undefined) return why;
  }
  if (env.code === "turn_failed") {
    const why = turnFailed(host, env);
    if (why !== undefined) return why;
  }
  return env.kind === "reply" &&
    env.ask_id !== undefined &&
    host.answered.has(env.ask_id)
    ? invalid("a reply to an ask already bounced")
    : undefined;
}

function callerSent(
  host: HostFold,
  e: EventOf<"message_sent">,
  caller: { thread_id: string; branch_id: string; agent: string },
): Violation {
  if (!tenantTeam(e.data.envelope))
    return invalid("a caller's mail is for another tenant's host team");
  const here =
    caller.thread_id === e.thread_id && caller.branch_id === e.branch_id;
  if (host.hostMember || !here)
    return invalid(
      "a caller's mail names its own log, which is no host member's",
    );
  return caller.agent === host.agent
    ? undefined
    : invalid("a caller's agent is not its thread's agent");
}

function turnFailed(host: HostFold, env: MailEnvelope): Violation {
  if (
    !host.failing ||
    env.ask_id === undefined ||
    !host.turnAsks.includes(env.ask_id)
  )
    return invalid(
      "a turn_failed bounce names no unanswered ask of a failed turn",
    );
  if (host.error !== undefined && !sameJson(env.error, host.error))
    return invalid("one failed turn's bounces carry one error");
  return env.error !== undefined && env.error.code === host.code
    ? undefined
    : invalid("a turn_failed error's code is not its turn end's");
}

/**
 * Rules 52 and 55 for a receipt: a caller's receipt names this log, host team mail stays inside
 * its principal's tenant, and one host member turn takes mail of one sender class.
 */
export function checkHostReceived(
  fold: Fold,
  e: EventOf<"message_received">,
): Violation {
  const { host } = fold.team;
  const env = e.data.envelope;
  const to = env.to;
  const caller = to !== "team_log" && "caller" in to ? to.caller : undefined;
  if ((caller !== undefined || host.hostMember) && !tenantTeam(env))
    return invalid("mail of another tenant's principal in a host team");
  if (
    caller !== undefined &&
    (caller.thread_id !== e.thread_id || caller.branch_id !== e.branch_id)
  )
    return invalid("a receipt to a caller is not in that caller's log");
  const ordinary = env.kind === "message" || env.kind === "ask";
  const klass = senderClass(env);
  return host.hostMember &&
    ordinary &&
    host.turnClass !== undefined &&
    host.turnClass !== klass
    ? invalid("a host member's turn takes mail of one sender class")
    : undefined;
}

/** Rule 53: member_idle{turn_failed} closes a failed turn once its every ask is bounced. */
export function checkHostIdle(
  fold: Fold,
  e: EventOf<"member_idle">,
): Violation {
  const failure = e.data.turn_failed;
  if (failure === undefined) return undefined;
  const { host } = fold.team;
  if (!host.failing || host.turnAsks.length > 0)
    return invalid(
      "member_idle{turn_failed} before every ask of its failed turn bounced",
    );
  if (host.error !== undefined && !sameJson(failure, host.error))
    return invalid("its error is not the turn's bounces'");
  return failure.code === host.code
    ? undefined
    : invalid("its code is not its turn end's");
}

/** Rule 54: `failed` closes an ask whose turn_failed bounce this log received, and only it. */
export function checkHostAskClosed(
  fold: Fold,
  e: EventOf<"ask_closed">,
): Violation {
  const { outcome, ask_id: ask } = e.data;
  const got = fold.team.host.bouncesIn.get(ask);
  if (outcome.status === "failed")
    return got !== undefined && sameJson(outcome.error, got)
      ? undefined
      : invalid("ask_closed{failed} names no received turn_failed bounce");
  return got === undefined
    ? undefined
    : invalid("a turn_failed bounce closes its ask failed");
}
