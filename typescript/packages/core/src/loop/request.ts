import { assertNever } from "../assert-never";
import type { EventOf } from "../fold/state";
import type { KnownEvent } from "../log";
import { deliverMessages } from "./agents/team";
import { attempt } from "./attempt";
import { refusal } from "./budget";
import { compact, reactiveSpent } from "./compact";
import { draft } from "./drafts";
import {
  batchGate,
  type Gated,
  inputGate,
  modelGate,
  resultsGate,
} from "./gates";
import { breakerOpen, ladder } from "./ladder";
import { switchGate } from "./lifecycle";
import { retryPolicy } from "./policy";
import { revert } from "./revert";
import type { Session } from "./session";
import { todoReminder } from "./todos";
import { cancelRequested, nextAttempt, stepEvents } from "./turn";
import { BARRED, type Halt } from "./types";

// A turn request: one attempt and what its outcome requires (// L5). Counters come from the step's events, so a recovered run keeps them.

type Abandon = EventOf<"model_attempt_abandoned">;
type Rejection = Abandon["data"]["reason"];

const RETRYABLE: ReadonlySet<Rejection> = new Set([
  "rate_limited",
  "overloaded",
  "server_error",
]);
const CRASH: ReadonlySet<Rejection> = new Set([
  "crash",
  "timeout",
  "stream_broken",
]);

/** Ends the turn with `reason`. */
export async function endTurn(
  s: Session,
  reason: EventOf<"turn_completed">["data"]["reason"],
): Promise<Halt | undefined> {
  return s.append(draft.turnCompleted(reason));
}

const abandons = (step: readonly KnownEvent[]): readonly Abandon[] =>
  step.filter((e): e is Abandon => e.type === "model_attempt_abandoned");

export async function requestTurn(s: Session): Promise<Halt | undefined> {
  // Every path to a turn's model call runs through here once its input is durable, fresh or
  // recovered: a turn-scoped fallback owed a revert gets it before anything renders. The revert
  // is its own step, so after_model_switch observes it before the request is sent.
  const reverted = await revert(s);
  if (reverted !== "none") return reverted;
  const step = stepEvents(s.events, s.fold);
  // A compaction side request's crashes are its own (manual.ts), never the turn's budget.
  const crashes = abandons(step).filter(
    (a) =>
      CRASH.has(a.data.reason) &&
      s.fold.requests.get(a.data.request_event_id)?.compaction !== true,
  );
  if (crashes.length > retryPolicy(s.fold.policy).crash_resends)
    return endTurn(s, "model_unavailable");
  await waitScheduled(s, step);
  const refused = refusal(s.events, s.fold.policy, s.now());
  if (refused !== undefined) {
    // The run ends before its ladder: an unanswered compaction request is answered with the
    // refusal, so it never outlives the run that should have carried it out.
    const request = s.fold.compactionRequest;
    const answered =
      request === undefined
        ? []
        : [
            draft.compactionFailed({
              stage: "summary",
              reason: "model_error",
              cause_event_id: request.event_id,
            }),
          ];
    const stopped = await s.append(draft.budgetExceeded(refused), ...answered);
    return stopped ?? (await endTurn(s, "budget_exhausted"));
  }
  const gated = await gates(s);
  if (gated !== undefined) return gated === "ended" ? undefined : gated;
  // A compaction side request the budget refused already recorded why.
  if (s.fold.budgetBlocked) return endTurn(s, "budget_exhausted");
  const got = await attempt(s, "turn", nextAttempt(s.events, s.fold));
  // A cancel that landed during the attempt: its outcome is recorded, and nothing more happens
  // in this step (no retry, fallback or sleep); the cancellation step is next.
  if (got.kind !== "halt" && cancelRequested(s.events, s.fold) !== undefined)
    return undefined;
  switch (got.kind) {
    case "halt":
      return got.halt;
    case "response":
    case "broken":
      return undefined;
    case "rejected":
      return rejected(s, got.rejection.reason);
    case "unsupported":
      return s.append(draft.turnCompleted("error", got.refused.code));
    case "leaked":
      // Recorded by the attempt: the turn ended with it, or a cancel closes it next.
      return undefined;
    case "budget":
      return endTurn(s, "budget_exhausted");
    case "barred":
      // Nothing was sent: the cancellation step is next.
      return undefined;
    default:
      return assertNever(got);
  }
}

/**
 * What stands between a step and its turn request, in order: the input guardrail, the
 * tool-output guardrail, after_tool_batch, the context ladder, then before_model.
 */
async function gates(s: Session): Promise<Gated> {
  return (
    (await inputGate(s)) ??
    (await todoReminder(s)) ??
    (await deliverMessages(s)) ??
    (await resultsGate(s)) ??
    (await batchGate(s)) ??
    (await ladder(s)) ??
    (await modelGate(s))
  );
}

/** Recovery after a crash waits only until a recorded retry's not_before. */
async function waitScheduled(
  s: Session,
  step: readonly KnownEvent[],
): Promise<void> {
  const last = step.at(-1);
  if (last?.type === "retry_scheduled" && last.data.not_before > s.now())
    await s.config.clock.sleepUntil(last.data.not_before);
}

async function rejected(
  s: Session,
  reason: Rejection,
): Promise<Halt | undefined> {
  if (reason === "prompt_too_long") return reactive(s);
  if (!RETRYABLE.has(reason)) return endTurn(s, "error");
  const step = stepEvents(s.events, s.fold);
  const retry = retryPolicy(s.fold.policy);
  const all = abandons(step);
  const rejections = all.filter((a) => RETRYABLE.has(a.data.reason));
  if (rejections.length > retry.max_retries)
    return endTurn(s, "model_unavailable");
  const last = all.at(-1);
  if (last === undefined) throw new Error("a rejection was just recorded");
  if (
    reason === "overloaded" &&
    overloadedInEpoch(step) >= retry.fallback_after
  ) {
    const fell = await fallBack(s, last);
    if (fell !== "retry") return fell;
  }
  return schedule(s, step, last, rejections.length);
}

/**
 * The next fallback epoch, gated by before_model_switch. "retry" when there is none or the hook
 * denied it: the attempt is retried on the current epoch.
 */
async function fallBack(
  s: Session,
  last: Abandon,
): Promise<Halt | undefined | "retry"> {
  const next = fallbackSettings(s);
  if (next === undefined) return "retry";
  const gate = await switchGate(s, next);
  if (!gate.allowed) return (await s.append(...gate.decisions)) ?? "retry";
  const switched = await s.appendWork(
    ...gate.decisions,
    draft.settingsChanged({
      reason: "fallback",
      settings: next,
      cause_event_id: last.event_id,
    }),
  );
  // A cancel landed during the hook: no switch; the cancellation step is next.
  return switched === BARRED ? undefined : switched;
}

/** Consecutive overloaded rejections since this step's last settings change. */
function overloadedInEpoch(step: readonly KnownEvent[]): number {
  const since = step.findLastIndex((e) => e.type === "settings_changed");
  return abandons(step.slice(since + 1)).filter(
    (a) => a.data.reason === "overloaded",
  ).length;
}

/** The next policy.fallback entry after the fallbacks already taken since the primary. */
function fallbackSettings(
  s: Session,
): EventOf<"settings_changed">["data"]["settings"] | undefined {
  const events = s.events;
  const primary = events.findLastIndex(
    (e) => e.type === "settings_changed" && e.data.reason !== "fallback",
  );
  const taken = events
    .slice(primary + 1)
    .filter(
      (e) => e.type === "settings_changed" && e.data.reason === "fallback",
    ).length;
  return s.fold.policy?.fallback?.[taken];
}

async function schedule(
  s: Session,
  step: readonly KnownEvent[],
  last: Abandon,
  k: number,
): Promise<Halt | undefined> {
  const retry = retryPolicy(s.fold.policy);
  const after = last.data.retry_after_ms;
  if (after !== undefined && after > retry.max_retry_after_ms)
    return endTurn(s, "model_unavailable");
  const delay =
    after ?? Math.min(retry.max_delay_ms, retry.base_delay_ms * 2 ** (k - 1));
  const waited = step.reduce(
    (sum, e) => (e.type === "retry_scheduled" ? sum + e.data.delay_ms : sum),
    0,
  );
  if (waited + delay > retry.max_total_wait_ms)
    return endTurn(s, "model_unavailable");
  const notBefore = s.now() + delay;
  const stopped = await s.appendWork(
    draft.retryScheduled({
      request_event_id: last.data.request_event_id,
      delay_ms: delay,
      not_before: notBefore,
      basis: after === undefined ? "backoff" : "retry_after",
    }),
  );
  // A cancel landed first: no wait; the cancellation step is next.
  if (stopped === BARRED) return undefined;
  if (stopped !== undefined) return stopped;
  await s.config.clock.sleepUntil(notBefore);
  return undefined;
}

/**
 * once per step, clear and compact (trigger reactive), then the request is sent
 * again. A second rejection, an open breaker or a failed compaction ends the turn.
 */
async function reactive(s: Session): Promise<Halt | undefined> {
  // Once per step, and never past an open breaker.
  if (reactiveSpent(s) || breakerOpen(s))
    return endTurn(s, "context_exhausted");
  const done = await compact(s, "reactive");
  if (done.kind === "halt") return done.halt;
  return done.kind === "compacted" || done.kind === "ended"
    ? undefined
    : endTurn(s, "context_exhausted");
}
