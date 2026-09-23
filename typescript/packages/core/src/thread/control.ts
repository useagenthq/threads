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
import { knownEvents } from "../reduce";
import { err, ok, type Result } from "../result";
import type { EventDraft, LogStore, Writer } from "../store";
import type { ChainEvent, LogError } from "../verify";

// The Thread control methods (spec/api.json Thread, ): each appends the actor's
// event under a short lease of its own, so it runs between runs, never beside one. The host
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
    | "invalid_transition";
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
const POLL_MS = 20;

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

/**
 * Takes the lease (waiting while a run holds it), appends the plan's events in one transaction,
 * and hands the lease back.
 */
export async function control(
  log: LogStore,
  branchId: BranchId,
  principal: Principal,
  plan: (
    events: readonly KnownEvent[],
    writer: Writer,
  ) => Result<Plan, ControlError>,
): Promise<Controlled> {
  if (principal.tenant !== log.tenant)
    return fail("forbidden", "the principal is not of this thread's tenant");
  const writer = await lease(log, branchId);
  if (!writer.ok) return fail("not_found", writer.error.message);
  try {
    const planned = plan(knownEvents(writer.value.chain), writer.value);
    if (!planned.ok) return planned;
    const { record, after } = planned.value;
    const done = writer.value.fenced(() => {
      const first = writer.value.append([record]);
      if (!first.ok || after === undefined) return first;
      const id = eventIdOf(first.value[0]);
      const rest = writer.value.append(after(id));
      return rest.ok ? first : rest;
    });
    if (!done.ok) return fail(codeOf(done.error), done.error.message);
    return ok({ event_id: eventIdOf(done.value[0]) });
  } finally {
    writer.value.release();
  }
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
      return error.code;
    default:
      return "invalid_request";
  }
}

// ponytail: polls a busy lease; a host cancels its own in-process run before calling this.
async function lease(
  log: LogStore,
  branchId: BranchId,
): Promise<Result<Writer, LogError>> {
  for (;;) {
    const writer = log.acquire(branchId, HOLDER);
    if (writer.ok || writer.error.code !== "branch_busy") return writer;
    const { promise, resolve } = Promise.withResolvers<void>();
    setTimeout(resolve, POLL_MS);
    await promise;
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
    // No rule is suggested yet, so none can be remembered.
    if (answer.grant && answer.rememberRule !== undefined)
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
    const after = resumeIf(writer, { kind: "approval", id: challengeId });
    return ok(after === undefined ? { record } : { record, after });
  };
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

/** answer: the ask_user call's result, from the answering principal. */
export function answer(
  callId: string,
  text: string | readonly string[],
  principal: Principal,
): (
  events: readonly KnownEvent[],
  writer: Writer,
) => Result<Plan, ControlError> {
  return (_events, writer) => {
    const address: ParkAddress = { kind: "input", id: callId };
    const after = resumeIf(writer, address);
    if (after === undefined)
      return err({
        code: "no_open_question",
        message: `no question ${callId}`,
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
