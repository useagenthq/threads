import {
  type EventOf,
  type Fold,
  loopParked,
  loopPending,
} from "../fold/state";
import type { KnownEvent, ToolSpec } from "../log";
import type { EventDraft } from "../store";
import { maxPauseContinuations } from "./policy";

// What the open turn looks like, read from the log. The loop decides every step from these,
// so a fresh run and a recovered one continue the same way (invariant 1).

export type Response = EventOf<"model_response" | "model_response_recovered">;

export type Step =
  | { readonly kind: "idle" }
  | { readonly kind: "parked" }
  | { readonly kind: "cancel"; readonly request: EventOf<"cancel_requested"> }
  | { readonly kind: "calls" }
  | { readonly kind: "request" }
  | { readonly kind: "respond"; readonly response: Response }
  | { readonly kind: "end_turn" };

/**
 * The events of the open (or last) turn, from the event that opened it: a user_input, a woken or
 * a received mail (spec/schema/README.md, "Which events open a turn").
 */
export function turnEvents(
  events: readonly KnownEvent[],
  fold: Pick<Fold, "turnStart">,
): readonly KnownEvent[] {
  const start = events.findLastIndex((e) => e.seq === fold.turnStart);
  return start === -1 ? [] : events.slice(start);
}

function isTurnRequest(e: KnownEvent): e is EventOf<"model_request"> {
  return e.type === "model_request" && e.data.purpose !== "compaction";
}

function isTurnResponse(e: KnownEvent, fold: Fold): e is Response {
  return (
    (e.type === "model_response" || e.type === "model_response_recovered") &&
    fold.requests.get(e.data.request_event_id)?.compaction !== true
  );
}

/** Events of the current step: after the last turn response, else the turn's input. */
export function stepEvents(
  events: readonly KnownEvent[],
  fold: Fold,
): readonly KnownEvent[] {
  const turn = turnEvents(events, fold);
  const last = turn.findLastIndex((e) => isTurnResponse(e, fold));
  return turn.slice(last + 1);
}

/** attempt of the next turn request: one more than the step's attempts so far. */
export function nextAttempt(events: readonly KnownEvent[], fold: Fold): number {
  return stepEvents(events, fold).filter(isTurnRequest).length + 1;
}

/** The latest tool set before the next call: the last tools_changed, else thread_started. */
export function toolSpec(fold: Fold, name: string): ToolSpec | undefined {
  return fold.tools.find((t) => t.name === name);
}

/** The spec a recorded call was made under; a later tools_changed never reclasses it. */
export function callSpec(
  fold: Fold,
  call: EventOf<"tool_call">,
): ToolSpec | undefined {
  return fold.calls.get(call.data.call_id)?.spec;
}

/** A cancel_requested barrier in the open turn: nothing new starts after it. */
export function cancelRequested(
  events: readonly KnownEvent[],
  fold: Pick<Fold, "turnStart">,
): EventOf<"cancel_requested"> | undefined {
  const cancel = turnEvents(events, fold).findLast(
    (e) => e.type === "cancel_requested",
  );
  return cancel?.type === "cancel_requested" ? cancel : undefined;
}

export function nextStep(events: readonly KnownEvent[], fold: Fold): Step {
  if (!fold.turnOpen) return { kind: "idle" };
  if (loopParked(fold).length > 0) return { kind: "parked" };
  const cancel = cancelRequested(events, fold);
  if (cancel !== undefined) return { kind: "cancel", request: cancel };
  const turn = turnEvents(events, fold);
  const at = turn.findLastIndex((e) => isTurnResponse(e, fold));
  const response = turn[at];
  const after = turn.slice(at + 1);
  // Every call of a response is recorded and authorized before any runs, also after a crash.
  const owed = owedCalls(events, fold);
  if (owed !== undefined && owed.parts.length > 0)
    return { kind: "respond", response: owed.response };
  if (loopPending(fold).length > 0) return { kind: "calls" };
  if (response === undefined || !isTurnResponse(response, fold))
    return { kind: "request" };
  if (after.some(isTurnRequest)) return { kind: "request" };
  if (!after.some((e) => e.type === "tool_call" || e.type === "injected"))
    return resumesPause(turn, response, fold)
      ? { kind: "request" }
      : { kind: "respond", response };
  return endsTurn(after, fold) ? { kind: "end_turn" } : { kind: "request" };
}

export type ToolUse = Extract<
  Response["data"]["content"][number],
  { type: "tool_use" }
>;

/** A response's part still owed its tool_call, or its recorded call still owed a decision. */
export type OwedPart =
  | { readonly kind: "unrecorded"; readonly use: ToolUse }
  | { readonly kind: "undecided"; readonly call: EventOf<"tool_call"> };

/**
 * The open turn's latest response and its parts still owed, in part order: a run stopped
 * mid-recording, or a log whose calls were all recorded before any was authorized.
 */
export function owedCalls(
  events: readonly KnownEvent[],
  fold: Fold,
):
  | { readonly response: Response; readonly parts: readonly OwedPart[] }
  | undefined {
  const turn = turnEvents(events, fold);
  const at = turn.findLastIndex((e) => isTurnResponse(e, fold));
  const response = turn[at];
  if (response === undefined || !isTurnResponse(response, fold)) return;
  const after = turn.slice(at + 1);
  const decided = new Set(
    after.flatMap((e) =>
      e.type === "permission_decision" ? [e.data.call_id] : [],
    ),
  );
  const parts = response.data.content.flatMap((use): OwedPart[] => {
    if (use.type !== "tool_use") return [];
    const call = after.find(
      (e): e is EventOf<"tool_call"> =>
        e.type === "tool_call" && e.data.call_id === use.call_id,
    );
    if (call === undefined) return [{ kind: "unrecorded", use }];
    return fold.pending.has(use.call_id) &&
      !fold.hostCalls.has(use.call_id) &&
      !decided.has(use.call_id)
      ? [{ kind: "undecided", call }]
      : [];
  });
  return { response, parts };
}

/**
 * pause_turn: ask again with nothing added, so the paused content goes back as-is, while the
 * turn's pauses stay within the cap. Past it, respond ends the turn.
 */
function resumesPause(
  turn: readonly KnownEvent[],
  response: Response,
  fold: Fold,
): boolean {
  if (
    response.data.stop_reason !== "pause_turn" ||
    response.data.content.some((p) => p.type === "tool_use")
  )
    return false;
  const pauses = turn.filter(
    (e) => isTurnResponse(e, fold) && e.data.stop_reason === "pause_turn",
  ).length;
  return pauses <= maxPauseContinuations(fold.policy);
}

/** A tool with ends_turn returned a success: no further model call. */
function endsTurn(after: readonly KnownEvent[], fold: Fold): boolean {
  return after.some((e) => {
    if (e.type !== "tool_result" || e.data.is_error) return false;
    const call = after.find(
      (c) => c.type === "tool_call" && c.data.call_id === e.data.call_id,
    );
    return (
      call?.type === "tool_call" && callSpec(fold, call)?.ends_turn === true
    );
  });
}

/**
 * The cancel barrier (spec/schema/README.md, "Nothing new after a barrier"): with a cancel pending
 * in the open turn, a batch that starts new work is refused (only its hook decisions are kept),
 * and any other batch keeps what it owes (an abandonment, a side request's compaction_failed) but
 * no turn_completed other than cancelled. The cancellation step closes the turn.
 */
export function afterBarrier(
  fold: Fold,
  events: readonly KnownEvent[],
  drafts: readonly EventDraft[],
): readonly EventDraft[] {
  if (!fold.turnOpen || cancelRequested(events, fold) === undefined)
    return drafts;
  // Refused whole, but the hooks that ran for it stay on record.
  if (drafts.some(opensWork))
    return drafts.filter((d) => d.type === "hook_decision");
  return drafts.filter(
    (d) => d.type !== "turn_completed" || d.data.reason === "cancelled",
  );
}

/**
 * Events that start new work: a request, a dispatch, a handoff target, a child, a retry wait, a
 * model switch. Teams' member starts and deliveries join this list when they become writable.
 */
export const OPENS_WORK: ReadonlySet<EventDraft["type"]> = new Set([
  "model_request",
  "effect_begin",
  "handoff",
  "agent_spawned",
  "retry_scheduled",
  "settings_changed",
]);

export const opensWork = (d: EventDraft): boolean => OPENS_WORK.has(d.type);
