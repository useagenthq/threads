import type { KnownEvent, MailEnvelope, TurnFailure } from "../log";
import type { EventOf } from "./state";

// The Teams Phase 2 part of one log's fold (spec/schema/README.md, "Teams Phase 2"): what
// semantic rules 50 and 52-55 need about a host team's log, a host member's log and its callers.
// Reference: spec/tools/fixtures/ref_host.py::HostCheck.

/** A host member's turn that ends any other way failed: only that turn ends (rule 53). */
export const TURN_KEPT: ReadonlySet<string> = new Set([
  "end_turn",
  "cancelled",
]);

/** The turn end reasons a TurnFailure.code passes through unchanged (rule 53's table). */
const PASSED_THROUGH: ReadonlySet<string> = new Set([
  "max_turns",
  "max_output",
  "stop_hook_limit",
  "context_exhausted",
  "output_invalid",
  "input_denied",
  "model_unavailable",
  "budget_exhausted",
  "interrupted",
]);

/**
 * What a host member's failed turn records, from its turn_completed (rule 53): the reason, or for
 * `error` its code, else `model_error`. `end_turn` and `cancelled` are not failures, and a
 * `handoff` can't happen (setup refuses handoffs on a host member), so it maps to no code.
 */
export function turnFailureCode(
  d: EventOf<"turn_completed">["data"],
): string | undefined {
  if (d.reason === "error") return d.code ?? "model_error";
  return PASSED_THROUGH.has(d.reason) ? d.reason : undefined;
}

export type HostFold = {
  /** The log opens with team_opened{kind: host} (rules 50, 51). */
  hostTeam: boolean;
  /** The log opens with thread_started{host_member} (rules 50, 52, 53, 55). */
  hostMember: boolean;
  /** thread_started.agent_name: the agent a caller's own address must name (rule 52). */
  agent: string | undefined;
  /** The latest generation of each host member this log started (rule 51). */
  readonly generations: Map<string, number>;
  /** Each decided generation's action, by generationKey: one decision per end (rule 51). */
  readonly decided: Map<string, string>;
  /** The times of each name's `restart` decisions, for the window count (rule 51). */
  readonly restarts: Map<string, number[]>;
  /** The previous event's type: a supervised restart follows its decision directly (rule 51). */
  lastType: string | undefined;
  /** A failed turn ended and its member_idle{turn_failed} is due (rule 53). */
  failing: boolean;
  /** That turn's error, once its first bounce named it (rule 53). */
  error: TurnFailure | undefined;
  /** The code the failed turn's end maps to; undefined for an unreachable end (rule 53). */
  code: string | undefined;
  /** Asks received in the open turn that this log has not answered (rule 53). */
  turnAsks: string[];
  /** Asks this log replied to or bounced: a later reply to one is refused (rule 53). */
  readonly answered: Set<string>;
  /** The sender class of the open turn's ordinary mail (rule 55). */
  turnClass: string | undefined;
  /** turn_failed bounces this log received, by ask id: their error (rule 54). */
  readonly bouncesIn: Map<string, TurnFailure>;
};

export function emptyHost(): HostFold {
  return {
    hostTeam: false,
    hostMember: false,
    agent: undefined,
    generations: new Map(),
    decided: new Map(),
    restarts: new Map(),
    lastType: undefined,
    failing: false,
    error: undefined,
    code: undefined,
    turnAsks: [],
    answered: new Set(),
    turnClass: undefined,
    bouncesIn: new Map(),
  };
}

/** One host member generation, as a Map key (rule 51). */
export function generationKey(name: string, generation: number): string {
  return `${name}/${generation}`;
}

/**
 * Who decided a mail to a host member (rule 55): a host rule is keyed by the sender's agent, so
 * one caller agent, one member name, or the operator.
 */
export function senderClass(env: MailEnvelope): string {
  const from = env.from;
  if ("caller" in from) return `caller:${from.caller.agent}`;
  return "operator" in from ? "operator" : `member:${from.name}`;
}

/** Advances the Phase 2 bookkeeping past one event that passed validate_next. */
export function applyHost(host: HostFold, e: KnownEvent): void {
  // team_opened and thread_started are only ever the resolved chain's first event (rule 33).
  opened(host, e);
  // Set before the branches: when the next event is checked this is the previous event's type,
  // which is what rule 51's "directly follows its decision" reads.
  host.lastType = e.type;
  if (e.type === "member_started")
    host.generations.set(e.data.member.name, e.data.member.generation);
  else if (e.type === "supervisor_decided") decided(host, e);
  else if (e.type === "message_received") received(host, e.data.envelope);
  else if (e.type === "message_sent") sent(host, e.data.envelope);
  else if (e.type === "turn_completed") turnEnded(host, e.data);
  else if (e.type === "member_idle") {
    if (e.data.turn_failed !== undefined) clearTurn(host);
  }
  // A member_ended in a failed turn's append ends the member instead, so the asks it had taken
  // and left unanswered close at their deadlines (rule 53).
  else if (e.type === "member_ended") clearTurn(host);
}

function decided(host: HostFold, e: EventOf<"supervisor_decided">): void {
  const { name, generation } = e.data.member;
  host.decided.set(generationKey(name, generation), e.data.action);
  if (e.data.action !== "restart") return;
  const times = host.restarts.get(name);
  if (times === undefined) host.restarts.set(name, [e.time]);
  else times.push(e.time);
}

function opened(host: HostFold, e: KnownEvent): void {
  if (e.type === "team_opened") host.hostTeam = e.data.kind === "host";
  else if (e.type === "thread_started") {
    host.hostMember = e.data.host_member !== undefined;
    host.agent = e.data.agent_name;
  }
}

function received(host: HostFold, env: MailEnvelope): void {
  const ordinary = env.kind === "message" || env.kind === "ask";
  if (host.hostMember && ordinary && host.turnClass === undefined)
    host.turnClass = senderClass(env);
  if (host.hostMember && env.kind === "ask" && env.ask_id !== undefined)
    host.turnAsks.push(env.ask_id);
  if (
    env.kind === "bounce" &&
    env.code === "turn_failed" &&
    env.ask_id !== undefined &&
    env.error !== undefined
  )
    host.bouncesIn.set(env.ask_id, env.error);
}

function sent(host: HostFold, env: MailEnvelope): void {
  if (env.code === "turn_failed" && env.error !== undefined)
    host.error = env.error;
  const answers = env.kind === "reply" || env.kind === "bounce";
  if (!answers || env.ask_id === undefined) return;
  host.answered.add(env.ask_id);
  host.turnAsks = host.turnAsks.filter((a) => a !== env.ask_id);
}

function turnEnded(host: HostFold, d: EventOf<"turn_completed">["data"]): void {
  if (host.hostMember && !TURN_KEPT.has(d.reason)) {
    host.failing = true;
    host.error = undefined;
    host.code = turnFailureCode(d);
    return;
  }
  clearTurn(host);
}

function clearTurn(host: HostFold): void {
  host.failing = false;
  host.error = undefined;
  host.turnAsks = [];
  host.turnClass = undefined;
}
