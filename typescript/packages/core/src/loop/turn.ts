import type { EventOf, Fold } from "../fold/state";
import type { KnownEvent, ToolSpec } from "../log";
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

/** The events of the open turn, from its user_input. */
export function turnEvents(
  events: readonly KnownEvent[],
): readonly KnownEvent[] {
  const start = events.findLastIndex((e) => e.type === "user_input");
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
  const turn = turnEvents(events);
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

/** A cancel_requested barrier in the open turn: nothing new starts after it. */
export function cancelRequested(
  events: readonly KnownEvent[],
): EventOf<"cancel_requested"> | undefined {
  const cancel = turnEvents(events).findLast(
    (e) => e.type === "cancel_requested",
  );
  return cancel?.type === "cancel_requested" ? cancel : undefined;
}

export function nextStep(events: readonly KnownEvent[], fold: Fold): Step {
  if (!fold.turnOpen) return { kind: "idle" };
  if (fold.parked.length > 0) return { kind: "parked" };
  const cancel = cancelRequested(events);
  if (cancel !== undefined) return { kind: "cancel", request: cancel };
  const turn = turnEvents(events);
  if (fold.pending.size > 0) return { kind: "calls" };
  const at = turn.findLastIndex((e) => isTurnResponse(e, fold));
  const response = turn[at];
  if (response === undefined || !isTurnResponse(response, fold))
    return { kind: "request" };
  const after = turn.slice(at + 1);
  if (after.some(isTurnRequest)) return { kind: "request" };
  if (!after.some((e) => e.type === "tool_call" || e.type === "injected"))
    return resumesPause(turn, response, fold)
      ? { kind: "request" }
      : { kind: "respond", response };
  return endsTurn(after, fold) ? { kind: "end_turn" } : { kind: "request" };
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
      call?.type === "tool_call" &&
      toolSpec(fold, call.data.name)?.ends_turn === true
    );
  });
}
