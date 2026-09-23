import type { z } from "zod";
import type { EventOf, ParkAddress } from "../fold/state";
import type {
  BranchId,
  EventId,
  JsonObject,
  KnownEvent,
  ModelRef,
  PermissionRule,
  Principal,
} from "../log";
import { principalKey } from "../log";
import { knownEvents } from "../reduce";
import { err, ok, type Result } from "../result";
import {
  type EventDraft,
  type LogStore,
  liveWriter,
  type Writer,
} from "../store";
import type { ChainEvent, LogError } from "../verify";
import { suggestedRules } from "./pending";

// The Thread control methods (spec/api.json Thread, ): each appends the actor's
// event through the run's own writer when this process runs the branch, else under a short lease
// of its own; a lease another process holds is branch_busy, never waited on. The host
// authorizes the principal first (approver policy, ); here a principal of
// another tenant is refused, and the log's own rules decide the rest.

/** A control method's typed failure (its api.json errors). */
export type ControlError = {
  readonly code:
    | "forbidden"
    | "not_found"
    | "invalid_request"
    | "approval_mismatch"
    | "approval_expired"
    | "approval_duplicate"
    | "no_open_question"
    | "not_parked"
    | "invalid_transition"
    | "branch_busy";
  readonly message: string;
};

/** host-api Appended: the control operation's durable record. */
export type Appended = { readonly event_id: EventId };

/** host-api SettingsChange. */
export type SettingsChange = {
  readonly model: z.infer<typeof ModelRef>;
  readonly model_params?: z.infer<typeof JsonObject>;
  readonly reasoning_carryover?: "keep" | "omit_prior";
};

type Controlled = Result<Appended, ControlError>;
/** What one control appends: its record, then what follows it (a resumed naming the record). */
export type Plan = {
  readonly record: EventDraft;
  readonly after?: (record: EventId) => readonly EventDraft[];
};

const HOLDER = `control-${crypto.randomUUID()}`;

const fail = (code: ControlError["code"], message: string): Controlled =>
  err({ code, message });

export function resumed(address: ParkAddress, cause: EventId): EventDraft {
  return {
    type: "resumed",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: { address, cause_event_id: cause },
  };
}

/** Host rows written in the same transaction as a control's record (a consumed inbox item). */
export type Alongside = Parameters<Writer["append"]>[1];

/**
 * Appends the plan's events in one transaction, through the live run's writer or under a lease
 * taken and handed back here.
 */
export async function control(
  log: LogStore,
  branchId: BranchId,
  principal: Principal,
  plan: (
    events: readonly KnownEvent[],
    writer: Writer,
  ) => Result<Plan, ControlError>,
  alongside?: Alongside,
): Promise<Controlled> {
  if (principal.tenant !== log.tenant)
    return fail("forbidden", "the principal is not of this thread's tenant");
  const live = liveWriter(branchId);
  if (live !== undefined) return appendPlan(live, plan, alongside);
  const writer = log.acquire(branchId, HOLDER);
  if (!writer.ok)
    return fail(
      writer.error.code === "branch_busy" ? "branch_busy" : "not_found",
      writer.error.message,
    );
  try {
    return appendPlan(writer.value, plan, alongside);
  } finally {
    writer.value.release();
  }
}

function appendPlan(
  writer: Writer,
  plan: (
    events: readonly KnownEvent[],
    writer: Writer,
  ) => Result<Plan, ControlError>,
  alongside?: Alongside,
): Controlled {
  const planned = plan(knownEvents(writer.chain), writer);
  if (!planned.ok) return planned;
  const { record, after } = planned.value;
  const done = writer.fenced(() => {
    const first = writer.append([record], alongside);
    if (!first.ok || after === undefined) return first;
    const id = eventIdOf(first.value[0]);
    const rest = writer.append(after(id));
    return rest.ok ? first : rest;
  });
  if (!done.ok) return fail(codeOf(done.error), done.error.message);
  return ok({ event_id: eventIdOf(done.value[0]) });
}

function eventIdOf(line: ChainEvent | undefined): EventId {
  if (line?.kind !== "event") throw new Error("a control appends known events");
  return line.event.event_id;
}

function codeOf(error: LogError): ControlError["code"] {
  switch (error.code) {
    case "approval_mismatch":
    case "approval_expired":
    case "approval_duplicate":
    case "invalid_transition":
    case "branch_busy":
      return error.code;
    default:
      return "invalid_request";
  }
}

function requested(
  events: readonly KnownEvent[],
  challengeId: string,
): EventOf<"approval_requested"> | undefined {
  return events.findLast(
    (e): e is EventOf<"approval_requested"> =>
      e.type === "approval_requested" && e.data.challenge_id === challengeId,
  );
}

/** The challenge's request, if it is open and unexpired. */
function openChallenge(
  events: readonly KnownEvent[],
  writer: Writer,
  challengeId: string,
  now: number,
): Result<EventOf<"approval_requested">, ControlError> {
  const asked = requested(events, challengeId);
  const state = writer.chain.fold.approvals.get(challengeId);
  if (asked === undefined || state === undefined)
    return err({ code: "not_found", message: `no challenge ${challengeId}` });
  if (state.consumed)
    return err({
      code: "approval_duplicate",
      message: `challenge ${challengeId} is already answered`,
    });
  return asked.data.expires_at <= now
    ? err({
        code: "approval_expired",
        message: `challenge ${challengeId} expired`,
      })
    : ok(asked);
}

/** approve and deny: the open challenge, answered once, before it expires. */
export function decide(
  challengeId: string,
  principal: Principal,
  now: number,
  answer:
    | {
        readonly grant: true;
        readonly rememberRule?: z.infer<typeof PermissionRule>;
      }
    | { readonly grant: false; readonly reason?: string },
): (
  events: readonly KnownEvent[],
  writer: Writer,
) => Result<Plan, ControlError> {
  return (events, writer) => {
    const open = openChallenge(events, writer, challengeId, now);
    if (!open.ok) return open;
    const asked = open.value;
    const rule = answer.grant ? answer.rememberRule : undefined;
    if (rule !== undefined && !suggested(events, asked.data.call_id, rule))
      return err({
        code: "invalid_request",
        message: "remember_rule must be one of the challenge's suggested_rules",
      });
    const binding = {
      challenge_id: challengeId,
      call_id: asked.data.call_id,
      args_hash: asked.data.args_hash,
    };
    const record: EventDraft = answer.grant
      ? {
          type: "approval_granted",
          type_version: 1,
          critical: true,
          actor: { kind: "approver", principal },
          data: binding,
        }
      : {
          type: "approval_denied",
          type_version: 1,
          critical: true,
          actor: { kind: "approver", principal },
          data: {
            ...binding,
            ...(answer.reason === undefined ? {} : { reason: answer.reason }),
          },
        };
    const resume = resumeIf(writer, { kind: "approval", id: challengeId });
    if (rule === undefined)
      return ok(resume === undefined ? { record } : { record, after: resume });
    const added: EventDraft = {
      type: "permission_rule_added",
      type_version: 1,
      critical: true,
      actor: { kind: "approver", principal },
      data: { rule, decision: "allow", challenge_id: challengeId },
    };
    return ok({
      record,
      after: (id) => [added, ...(resume === undefined ? [] : resume(id))],
    });
  };
}

function suggested(
  events: readonly KnownEvent[],
  callId: string,
  rule: string,
): boolean {
  const call = events.find(
    (e) => e.type === "tool_call" && e.data.call_id === callId,
  );
  return (
    call?.type === "tool_call" &&
    suggestedRules(call.data.name, call.data.input).includes(rule)
  );
}

/** A resumed for `address` when the branch is parked on it. */
function resumeIf(
  writer: Writer,
  address: ParkAddress,
): ((id: EventId) => readonly EventDraft[]) | undefined {
  const parked = writer.chain.fold.parked.some(
    (a) => a.kind === address.kind && a.id === address.id,
  );
  return parked ? (id) => [resumed(address, id)] : undefined;
}

/** Whether `principal` gave the user_input that opened the turn with the question's call. */
function asker(
  events: readonly KnownEvent[],
  callId: string,
  principal: Principal,
): boolean {
  const at = events.findIndex(
    (e) => e.type === "tool_call" && e.data.call_id === callId,
  );
  const opener = events
    .slice(0, Math.max(at, 0))
    .findLast((e) => e.type === "user_input")?.actor.principal;
  return (
    opener !== undefined && principalKey(opener) === principalKey(principal)
  );
}

/** answer: the ask_user call's result, from the asking principal. */
export function answer(
  callId: string,
  text: string | readonly string[],
  principal: Principal,
): (
  events: readonly KnownEvent[],
  writer: Writer,
) => Result<Plan, ControlError> {
  return (events, writer) => {
    const address: ParkAddress = { kind: "input", id: callId };
    const after = resumeIf(writer, address);
    if (after === undefined)
      return err({
        code: "no_open_question",
        message: `no question ${callId}`,
      });
    if (!asker(events, callId, principal))
      return err({
        code: "forbidden",
        message: "only the user whose input opened this turn may answer",
      });
    return ok({
      record: {
        type: "tool_result",
        type_version: 1,
        critical: true,
        actor: { kind: "user", principal },
        data: {
          call_id: callId,
          is_error: false,
          origin: "answered",
          completeness: "complete",
          preview: typeof text === "string" ? text : text.join("\n"),
        },
      },
      after,
    });
  };
}

/** resolveParked: a human settles a parked effect. */
export function resolveParked(
  effectKey: string,
  resolution: "assume_done" | "assume_not_done",
  principal: Principal,
): (
  events: readonly KnownEvent[],
  writer: Writer,
) => Result<Plan, ControlError> {
  return (_events, writer) => {
    const address: ParkAddress = { kind: "effect", id: effectKey };
    const after = resumeIf(writer, address);
    const effect = writer.chain.fold.effects.get(effectKey);
    if (after === undefined || effect === undefined)
      return err({ code: "not_parked", message: `${effectKey} is not parked` });
    return ok({
      record: {
        type: "effect_resolved",
        type_version: 1,
        critical: true,
        actor: { kind: "approver", principal },
        data: { call_id: effect.callId, outcome: resolution, by: "human" },
      },
      after,
    });
  };
}
