import { assertNever } from "../assert-never";
import { type EventOf, type Fold, responseText } from "../fold/state";
import type { KnownEvent } from "../log";
import { attempt } from "./attempt";
import {
  type Answering,
  beforeCompact,
  type Compaction,
  clear,
  type FailReason,
  failed,
  summarized,
} from "./compact";
import { RECOVERY } from "./drafts";
import type { Gated } from "./gates";
import { retryPolicy } from "./policy";
import type { Session } from "./session";
import type { Halt } from "./types";

// A requested compaction (Thread.compact): carried out at the next context ladder, before
// anything else, and answered by exactly one outcome. Every step is decided from the log by
// `stage`, so the live loop and a resumed run make the same decision (spec/schema/README.md,
// "Requested compaction and output styles").

type Request = EventOf<"compaction_requested">;
type Side = EventOf<"model_request">;

/** What the log says a request needs next. */
export type Stage =
  /** No side request yet: the remaining before_compact hooks, then attempt 1. */
  | { readonly kind: "start" }
  /** A crash abandonment within the crash_resends budget: attempt n+1. */
  | { readonly kind: "send"; readonly attempt: number }
  /** The first prompt_too_long: clear every clearable result, then attempt n+1. */
  | { readonly kind: "fallback"; readonly attempt: number }
  /** The latest attempt awaits its response: open-attempt recovery settles it first. */
  | { readonly kind: "open" }
  /** The recorded summary answers the request. */
  | { readonly kind: "summary"; readonly text: string }
  | { readonly kind: "failed"; readonly reason: FailReason };

export function stage(
  events: readonly KnownEvent[],
  fold: Fold,
  request: Request,
): Stage {
  const sides = events.filter(
    (e): e is Side =>
      e.type === "model_request" && e.data.cause_event_id === request.event_id,
  );
  const last = sides.at(-1);
  if (last === undefined) return { kind: "start" };
  if (fold.awaiting.has(last.event_id)) return { kind: "open" };
  const ids = new Set(sides.map((e) => e.event_id));
  const response = events.find(
    (e) =>
      (e.type === "model_response" || e.type === "model_response_recovered") &&
      e.data.request_event_id === last.event_id,
  );
  if (
    response?.type === "model_response" ||
    response?.type === "model_response_recovered"
  ) {
    const text = responseText(response.data.content);
    return text === ""
      ? { kind: "failed", reason: "empty_summary" }
      : { kind: "summary", text };
  }
  const abandons = events.filter(
    (e): e is EventOf<"model_attempt_abandoned"> =>
      e.type === "model_attempt_abandoned" && ids.has(e.data.request_event_id),
  );
  const reason = abandons.at(-1)?.data.reason;
  const count = abandons.filter((a) => a.data.reason === reason).length;
  const next = last.data.attempt + 1;
  if (reason === "crash")
    return count <= retryPolicy(fold.policy).crash_resends
      ? { kind: "send", attempt: next }
      : { kind: "failed", reason: "model_error" };
  // The one fallback is counted from the log, never from the attempt number, so a crash
  // re-send before it doesn't use it up.
  if (reason === "prompt_too_long")
    return count === 1
      ? { kind: "fallback", attempt: next }
      : { kind: "failed", reason: "prompt_too_long" };
  return { kind: "failed", reason: "model_error" };
}

/**
 * The ladder's first layer: the unanswered request, carried out until it is answered. "ended"
 * when it ran, so the loop takes its next step from the log: a cancel that landed during the side
 * attempt is handled before any turn request.
 */
export async function requested(s: Session): Promise<Gated> {
  if (s.fold.compactionRequest === undefined) return undefined;
  for (;;) {
    const request = s.fold.compactionRequest;
    if (request === undefined) return "ended";
    const stopped = await advance(s, request, stage(s.events, s.fold, request));
    if (stopped !== undefined) return stopped;
  }
}

/**
 * Recovery's part: an outcome the log already decides is appended before anything runs. A
 * stage that needs a model call is left to the ladder.
 */
export async function settleRequested(s: Session): Promise<Halt | undefined> {
  const request = s.fold.compactionRequest;
  if (request === undefined) return undefined;
  const next = stage(s.events, s.fold, request);
  if (next.kind !== "summary" && next.kind !== "failed") return undefined;
  return answer(s, request, next, RECOVERY);
}

async function advance(
  s: Session,
  request: Request,
  next: Stage,
): Promise<Halt | undefined> {
  switch (next.kind) {
    case "start": {
      const gated = await beforeCompact(s, request);
      if (gated !== undefined)
        return gated.kind === "halt" ? gated.halt : undefined;
      return send(s, request, 1);
    }
    case "send":
      return send(s, request, next.attempt);
    case "fallback":
      return (
        clear(s, 0, "compaction_fallback") ?? send(s, request, next.attempt)
      );
    case "open":
      throw new Error(
        "recovery settles an open side request before the loop runs",
      );
    case "summary":
    case "failed":
      return answer(s, request, next);
    default:
      return assertNever(next);
  }
}

/**
 * One side attempt. A response or a recorded rejection goes back to `stage`; a refusal that left
 * no request behind answers the request here.
 */
async function send(
  s: Session,
  request: Request,
  number: number,
): Promise<Halt | undefined> {
  const got = await attempt(s, "compaction", number, request.event_id);
  const cause = { cause: request.event_id };
  switch (got.kind) {
    case "response":
    case "rejected":
    case "broken":
    case "budget":
      return undefined;
    case "unsupported":
      return halted(failed(s, "model_error", cause));
    case "halt": {
      const code = got.halt.code;
      if (code === "artifact_missing" || code === "artifact_corrupt")
        return halted(failed(s, "artifact_error", cause));
      return code === "model_error"
        ? halted(failed(s, "model_error", cause))
        : got.halt;
    }
    default:
      return assertNever(got);
  }
}

async function answer(
  s: Session,
  request: Request,
  outcome: Extract<Stage, { kind: "summary" | "failed" }>,
  actor?: typeof RECOVERY,
): Promise<Halt | undefined> {
  const answering: Answering = {
    cause: request.event_id,
    ...(actor === undefined ? {} : { actor }),
  };
  if (outcome.kind === "failed")
    return halted(failed(s, outcome.reason, answering));
  const from = s.fold.firstInput;
  const to = s.events.find((e) => e.seq === request.seq - 1);
  if (from === undefined || to === undefined)
    throw new Error("a request follows an input and a known event (rule 30)");
  return halted(
    await summarized(s, [from, to], outcome.text, "manual", answering),
  );
}

function halted(c: Compaction): Halt | undefined {
  return c.kind === "halt" ? c.halt : undefined;
}
