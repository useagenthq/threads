import { assertNever } from "../assert-never";
import type { EventOf, Fold } from "../fold/state";
import { mailOpensTurn } from "../fold/team";
import { type KnownEvent, principalKey } from "../log";
import { invalid, type Violation } from "./violation";

// Semantic rules 31 and 33-45 on one log (spec/schema/README.md, "Semantic rules"; reference:
// spec/tools/fixtures/ref_rules.py). Rule 43 is cross-log and 32 is woken's (wake.ts).

const TEAM_LOG: ReadonlySet<KnownEvent["type"]> = new Set([
  "operator_request",
  "operator_refused",
  "message_policy_decided",
  "member_started",
  "message_sent",
  "message_received",
  "mail_refused",
  "ask_closed",
  "wait_started",
  "member_observed",
  "wait_finished",
]);

/** Rule 33: team_opened starts a branch, and a team log takes only the operator's side. */
export function checkTeamLog(fold: Fold, e: KnownEvent): Violation {
  if (e.type === "team_opened" && fold.eventIds.size > 0)
    return invalid("team_opened after the branch's first event");
  return fold.team.teamLog && !TEAM_LOG.has(e.type)
    ? invalid(`a team log takes no ${e.type}`)
    : undefined;
}

/** Rules 31, 34, 37 and 45 for a receipt. */
export function checkReceived(
  fold: Fold,
  e: EventOf<"message_received">,
): Violation {
  const env = e.data.envelope;
  if (e.data.mail_id !== env.mail_id)
    return invalid("a receipt's mail_id is not its envelope's");
  if (fold.team.mailDone.has(env.mail_id))
    return invalid(`mail ${env.mail_id} is received twice`);
  if (env.kind === "task")
    return invalid("a task arrives as user_input{team_task}, not as mail");
  const principal = principalKey(env.provenance.principal);
  if (principalKey(e.actor.principal) !== principal)
    return invalid("a receipt's actor is not its provenance principal");
  const { run } = fold.wake;
  const ordinary = env.kind === "message" || env.kind === "ask";
  const joins =
    run?.principal === principal &&
    run.root === env.provenance.root_request.event_id;
  if (ordinary && fold.turnOpen && !joins)
    return invalid("mail of another request joins the open turn");
  return fold.team.ended && mailOpensTurn(fold, env)
    ? invalid("an ended member's log opens a turn")
    : undefined;
}

/** Rule 31 for a refusal. */
export function checkRefused(
  fold: Fold,
  e: EventOf<"mail_refused">,
): Violation {
  return fold.team.mailDone.has(e.data.mail_id)
    ? invalid(`mail ${e.data.mail_id} is refused after it was taken`)
    : undefined;
}

/** Rules 35, 42 and 44 for mail this log sends. */
export function checkSent(fold: Fold, e: EventOf<"message_sent">): Violation {
  const env = e.data.envelope;
  // An AskId is its ask's MailId: the same string under two brands.
  const askId: string | undefined = env.ask_id;
  if (env.kind === "ask" && askId !== env.mail_id)
    return invalid("an ask's ask_id is not its own mail_id");
  if (
    env.kind === "reply" &&
    (env.ask_id === undefined || !fold.team.asksIn.has(env.ask_id))
  )
    return invalid("a reply names no ask this log received and left unreplied");
  const from = env.from;
  return fold.team.teamLog &&
    "operator" in from &&
    !fold.team.requests.has(from.operator)
    ? invalid("operator mail before its operator_request")
    : undefined;
}

/** Rule 36: an ask closes once, and an answer is a reply to it received here. */
export function checkAskClosed(
  fold: Fold,
  e: EventOf<"ask_closed">,
): Violation {
  const { ask_id: ask, outcome } = e.data;
  if (!fold.team.asksOut.has(ask))
    return invalid(`ask_closed closes no open ask ${ask} of this log`);
  return outcome.status === "answered" &&
    fold.team.repliesIn.get(outcome.reply) !== ask
    ? invalid("answered names no received reply to this ask")
    : undefined;
}

/** Rules 37 and 38: a member ends once, and idles only after its turn ended well. */
export function checkMemberEnd(
  fold: Fold,
  e: EventOf<"member_ended" | "member_idle">,
): Violation {
  if (fold.team.ended) return invalid(`${e.type} after member_ended`);
  return e.type === "member_idle" &&
    (fold.turnOpen || fold.team.lastEnd !== "end_turn")
    ? invalid("member_idle without its turn's turn_completed{end_turn}")
    : undefined;
}

/** Rule 39: a wait's members are a set; a wait finishes and a monitor is observed once. */
export function checkWaits(
  fold: Fold,
  e: EventOf<"wait_started" | "wait_finished" | "member_observed">,
): Violation {
  const { team } = fold;
  switch (e.type) {
    case "wait_started": {
      const keys = e.data.members.map((m) => `${m.name}#${m.generation}`);
      return new Set(keys).size === keys.length
        ? undefined
        : invalid("a wait lists a member twice");
    }
    case "wait_finished":
      return team.waits.has(e.data.wait_id)
        ? undefined
        : invalid(`wait_finished names no open wait ${e.data.wait_id}`);
    case "member_observed":
      return team.monitors.has(e.data.monitor_id)
        ? undefined
        : invalid(`member_observed names no live monitor ${e.data.monitor_id}`);
    default:
      return assertNever(e);
  }
}

/** Rule 40: a team park names an open ask, an unfinished wait or a start of this log. */
export function checkTeamPark(fold: Fold, e: EventOf<"parked">): Violation {
  const { kind, id } = e.data.address;
  const { team } = fold;
  const known =
    kind === "ask" || kind === "wait" || kind === "member"
      ? { ask: team.asksOut, wait: team.waits, member: team.taskMonitors }[kind]
      : undefined;
  return known === undefined || known.has(id)
    ? undefined
    : invalid(`a park on ${kind} ${id} names nothing of this log`);
}

/** Rules 31, 37 and 41: a member's task is its first input and only it; an ended log opens no
 * turn. */
export function checkTeamInput(
  fold: Fold,
  e: EventOf<"user_input">,
): Violation {
  const { team } = fold;
  if (team.ended) return invalid("an ended member's log opens a turn");
  const task = e.data.source === "team_task";
  const mail = e.data.mail_id;
  if (mail !== undefined && team.mailDone.has(mail))
    return invalid(`task ${mail} is taken twice`);
  if (!team.member)
    return task ? invalid("team_task input outside a member's log") : undefined;
  return task === team.hadInput
    ? invalid("a member's task is its first input, and only it")
    : undefined;
}

/** Rule 37: an ended log is never woken. */
export function checkNotEnded(fold: Fold): Violation {
  return fold.team.ended
    ? invalid("an ended member's log opens a turn")
    : undefined;
}

/** Rules 42 and 45: the operator side of a team log. */
export function checkOperator(
  fold: Fold,
  e: EventOf<
    "operator_request" | "operator_refused" | "message_policy_decided"
  >,
): Violation {
  const { team } = fold;
  switch (e.type) {
    case "operator_request": {
      const { provenance } = e.data;
      const who = principalKey(provenance.principal);
      const same =
        principalKey(e.actor.principal) === who &&
        principalKey(e.data.principal) === who;
      if (!same) return invalid("an operator_request's principals differ");
      return provenance.root_request.event_id === e.event_id
        ? undefined
        : invalid("an operator_request's root request is not itself");
    }
    case "operator_refused":
      return team.requests.has(e.data.request_id)
        ? undefined
        : invalid("operator_refused before its operator_request");
    case "message_policy_decided":
      return checkDecision(fold, e);
    default:
      return assertNever(e);
  }
}

function checkDecision(
  fold: Fold,
  e: EventOf<"message_policy_decided">,
): Violation {
  const { request_id: request, call_id: call } = e.data;
  if (request !== undefined)
    return fold.team.requests.has(request)
      ? undefined
      : invalid("message_policy_decided before its operator_request");
  return call !== undefined && fold.pending.has(call)
    ? undefined
    : invalid("message_policy_decided names no pending tool_call");
}

/** Rule 45: a lead's member_started is its member's parent; a team log's names the lead. */
export function checkStarted(
  fold: Fold,
  e: EventOf<"member_started">,
): Violation {
  const { parent } = e.data;
  if (fold.team.teamLog)
    return parent.thread_id === fold.team.leadThread
      ? undefined
      : invalid("an operator start's parent is not the lead's thread");
  const itself =
    parent.thread_id === e.thread_id &&
    parent.branch_id === e.branch_id &&
    parent.event_id === e.event_id;
  return itself
    ? undefined
    : invalid("a lead's member_started does not name itself as parent");
}
