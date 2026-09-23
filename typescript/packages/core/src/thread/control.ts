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

// The Thread control methods (spec/api.json Thread): each appends the actor's
// event through the run's own writer when this process runs the branch, else under a short lease
// of its own; a lease another process holds is branch_busy, never waited on. The host
// authorizes the principal first (approver policy); here a principal of
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
    | "branch_busy"
    | "branch_not_runnable";
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

export type ControlOptions = {
  readonly alongside?: Alongside;
  /**
   * Only between turns (compact, setOutputStyle): a run in progress, one parked on an approval
   * or a question included, is branch_busy, and an inspection-only branch is branch_not_runnable.
   */
  readonly idle?: boolean;
};

const BUSY = "the thread has a turn in progress; try again when it ends";

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
  { alongside, idle = false }: ControlOptions = {},
): Promise<Controlled> {
  if (principal.tenant !== log.tenant)
    return fail("forbidden", "the principal is not of this thread's tenant");
  const live = liveWriter(branchId);
  if (live !== undefined)
    return idle ? fail("branch_busy", BUSY) : appendPlan(live, plan, alongside);
  const writer = log.acquire(branchId, HOLDER);
  if (!writer.ok)
    return fail(acquireCode(writer.error, idle), writer.error.message);
  const planned = idle ? between(plan) : plan;
  try {
    return appendPlan(writer.value, planned, alongside);
  } finally {
    writer.value.release();
  }
}

/** The existing controls report a branch they can't run as not_found; the idle ones say why. */
function acquireCode(error: LogError, idle: boolean): ControlError["code"] {
  if (error.code === "branch_busy") return "branch_busy";
  return idle && error.code === "branch_not_runnable"
    ? "branch_not_runnable"
    : "not_found";
}

/** A parked run releases its lease, so the lease alone doesn't prove the thread is idle. */
function between(
  plan: (
    events: readonly KnownEvent[],
    writer: Writer,
  ) => Result<Plan, ControlError>,
): (
  events: readonly KnownEvent[],
  writer: Writer,
) => Result<Plan, ControlError> {
  return (events, writer) =>
    writer.chain.fold.turnOpen
      ? err({ code: "branch_busy", message: BUSY })
      : plan(events, writer);
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

/** A resumed for `address` when the branch is parked on it. */
export function resumeIf(
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

/** resolveParked: a human settles a parked effect (C3). */
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
