import type { EventOf } from "../fold/state";
import type { KnownEvent } from "../log";
import { refReader, render } from "../render";
import { clear, compact, reactiveSpent, windowTokens } from "./compact";
import { draft } from "./drafts";
import type { Gated } from "./gates";
import { contextPolicy, tokens } from "./policy";
import type { Session } from "./session";
import { stepEvents } from "./turn";

// The proactive layers of the context ladder, run before every turn request:
// L1 clears old results, L2 compacts (L3 restore follows it), L4 blocks a request that would
// reach the window before it exists. Each layer runs only while the estimate is still over its
// trigger, and each appends events: nothing is decided outside the log.

type Response = EventOf<"model_response" | "model_response_recovered">;

/** Events after which a recorded request's bytes are no longer the start of the next one. */
const RESHAPES: ReadonlySet<KnownEvent["type"]> = new Set([
  "compacted",
  "context_edited",
  "settings_changed",
]);

/**
 * an estimate (never a count): the input side of the epoch's last turn
 * response with known usage, plus bytes/4 of what the render added since its request. With no
 * such response, or when the context was reshaped since, bytes/4 of the whole next request.
 */
export function estimate(s: Session): number {
  const events = s.events;
  const next = render(events, refReader(s.artifacts));
  // An unrenderable request fails in the attempt, with its own error; it isn't estimated.
  if (!next.ok) return 0;
  const bytes = next.value.bytes.length;
  const whole = Math.ceil(bytes / 4);
  const epoch = events.findLastIndex(
    (e) => e.type === "thread_started" || e.type === "settings_changed",
  );
  const last = events
    .slice(epoch + 1)
    .findLast(
      (e): e is Response =>
        (e.type === "model_response" ||
          e.type === "model_response_recovered") &&
        e.data.usage.input_tokens !== null &&
        s.fold.requests.get(e.data.request_event_id)?.compaction !== true,
    );
  if (last === undefined) return whole;
  const request = events.find((e) => e.event_id === last.data.request_event_id);
  if (request?.type !== "model_request") return whole;
  if (events.some((e) => e.seq > request.seq && RESHAPES.has(e.type)))
    return whole;
  const { input_tokens, cache_read_tokens, cache_write_tokens } =
    last.data.usage;
  const added = Math.max(0, bytes - request.data.request_ref.bytes);
  return (
    (input_tokens ?? 0) +
    (cache_read_tokens ?? 0) +
    (cache_write_tokens ?? 0) +
    Math.ceil(added / 4)
  );
}

/** L1, L2 (then L3) and L4 before a turn request. */
export async function ladder(s: Session): Promise<Gated> {
  const window = windowTokens(s);
  // A model with no declared window can't be measured against one.
  if (window <= 0) return undefined;
  const ctx = contextPolicy(s.fold.policy);
  if (estimate(s) >= tokens(ctx.clear_results.trigger, window)) {
    const stopped = clear(s, ctx.clear_results.keep_recent, "threshold");
    if (stopped !== undefined) return stopped;
  }
  if (
    estimate(s) >= tokens(ctx.compact.trigger, window) &&
    !breakerOpen(s) &&
    !compactedThisStep(s)
  ) {
    const got = await compact(s, "threshold");
    if (got.kind === "halt") return got.halt;
    if (got.kind === "ended") return "ended";
  }
  const estimated = estimate(s);
  return estimated >= window ? preflight(s, estimated, window) : undefined;
}

/**
 * L4: no request exists, so nothing is sent, billed or counted. The step's one reactive
 * compaction runs (the guard L5 shares), then the request is built fresh; spent, it fails.
 */
async function preflight(
  s: Session,
  estimated: number,
  window: number,
): Promise<Gated> {
  const action = reactiveSpent(s) || breakerOpen(s) ? "fail" : "compact";
  const stopped = s.append(
    draft.preflightBlocked({
      estimated_tokens: estimated,
      window_tokens: window,
      action,
    }),
  );
  if (stopped !== undefined) return stopped;
  if (action === "fail")
    return s.append(draft.turnCompleted("context_exhausted")) ?? "ended";
  const got = await compact(s, "reactive");
  if (got.kind === "halt") return got.halt;
  return got.kind === "compacted" || got.kind === "ended"
    ? "ended"
    : (s.append(draft.turnCompleted("context_exhausted")) ?? "ended");
}

/** consecutive failures since the last compacted reach max_failures. */
export function breakerOpen(s: Session): boolean {
  return (
    s.fold.compactionFailures >=
    contextPolicy(s.fold.policy).compact.max_failures
  );
}

/** One threshold compaction per step: a second would summarize the summary. */
function compactedThisStep(s: Session): boolean {
  return stepEvents(s.events, s.fold).some(
    (e) => e.type === "compacted" || e.type === "compaction_failed",
  );
}
