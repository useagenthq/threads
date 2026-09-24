import { assertNever } from "../assert-never";
import type { EventOf } from "../fold/state";
import type { Outcome } from "../hooks/invoke";
import type { EventId, KnownEvent } from "../log";
import { refReader, render } from "../render";
import type { EventDraft } from "../store";
import { type Attempted, attempt } from "./attempt";
import { draft, HOST, type RECOVERY } from "./drafts";
import { decision, defining, recorded, run } from "./hooks";
import { contextPolicy, tokens } from "./policy";
import { restoreDrafts, restoreHooks } from "./restore";
import type { Session } from "./session";
import { cancelRequested, stepEvents } from "./turn";
import type { Halt } from "./types";

// (clear old results) and L2 (summary compaction, then L3 restore), run on a
// threshold by the ladder (ladder.ts), reactively by L4 and L5, or on request (manual.ts).

export type Compaction =
  | { readonly kind: "compacted" }
  | { readonly kind: "failed" }
  /** The summary request's response held a registered secret: the turn has ended. */
  | { readonly kind: "ended" }
  | { readonly kind: "halt"; readonly halt: Halt };

type Reason = "threshold" | "compaction_fallback";
export type FailReason =
  | "still_over_threshold"
  | "empty_summary"
  | "prompt_too_long"
  | "model_error"
  | "artifact_error";

/** Who a compaction's outcome is for: the request it answers, and recovery when it settles it. */
export type Answering = {
  readonly cause?: EventId;
  readonly actor?: typeof HOST | typeof RECOVERY;
};

/** L5's once-per-step guard, shared with L4: this step already compacted or failed to. */
export function reactiveSpent(s: Session): boolean {
  return stepEvents(s.events, s.fold).some(
    (e) => e.type === "compacted" || e.type === "compaction_failed",
  );
}

export async function compact(
  s: Session,
  trigger: "reactive" | "threshold",
): Promise<Compaction> {
  const ctx = contextPolicy(s.fold.policy);
  const cleared = clear(s, ctx.clear_results.keep_recent, "threshold");
  if (cleared !== undefined) return { kind: "halt", halt: cleared };
  const range = compactRange(s);
  if (range === undefined) return failed(s, "still_over_threshold");
  const gated = await beforeCompact(s);
  if (gated !== undefined) return gated;
  const got = await sideRequest(s);
  switch (got.kind) {
    case "halt":
      return got;
    case "response":
      return got.text === ""
        ? failed(s, "empty_summary")
        : summarized(s, range, got.text, trigger);
    case "rejected":
      return failed(
        s,
        got.rejection.reason === "prompt_too_long"
          ? "prompt_too_long"
          : "model_error",
      );
    case "leaked":
      // With a cancel pending the turn is still open: the compaction failed, and the
      // cancellation step closes the turn.
      return got.ended ? { kind: "ended" } : failed(s, "model_error");
    case "broken":
    case "unsupported":
      return failed(s, "model_error");
    case "budget":
      // The attempt recorded the failure with budget_exceeded.
      return { kind: "failed" };
    default:
      return assertNever(got);
  }
}

/**
 * The side request. If it is itself too long, everything clearable is cleared and it is retried
 * once, unless a cancel landed meanwhile: nothing is sent after the barrier, so the rejection
 * stands and the compaction fails.
 */
async function sideRequest(s: Session): Promise<Attempted> {
  const got = await attempt(s, "compaction", 1);
  if (
    got.kind !== "rejected" ||
    got.rejection.reason !== "prompt_too_long" ||
    cancelRequested(s.events) !== undefined
  )
    return got;
  const fallback = clear(s, 0, "compaction_fallback");
  if (fallback !== undefined) return { kind: "halt", halt: fallback };
  return attempt(s, "compaction", 2);
}

/** compaction_failed{summary}, naming the latest side request unless none was made for it. */
export function failed(
  s: Session,
  reason: FailReason,
  answering: Answering = {},
): Compaction {
  const { cause, actor = HOST } = answering;
  const side = s.events.findLast(
    (e) =>
      e.type === "model_request" &&
      e.data.purpose === "compaction" &&
      e.data.cause_event_id === cause,
  );
  const omit =
    side === undefined ||
    reason === "still_over_threshold" ||
    reason === "artifact_error";
  const stopped = s.append(
    draft.compactionFailed(
      {
        stage: "summary",
        reason,
        ...(omit ? {} : { request_event_id: side.event_id }),
        ...(cause === undefined ? {} : { cause_event_id: cause }),
      },
      actor,
    ),
  );
  return stopped === undefined
    ? { kind: "failed" }
    : { kind: "halt", halt: stopped };
}

/**
 * `compacted` over `range`, from the latest side request's summary, with its restore in the
 * same batch; the context hooks follow.
 */
export async function summarized(
  s: Session,
  range: readonly [KnownEvent, KnownEvent],
  text: string,
  trigger: "reactive" | "threshold" | "manual",
  answering: Answering = {},
): Promise<Compaction> {
  const [from, to] = range;
  const { cause, actor = HOST } = answering;
  const side = s.events.findLast(
    (e) => e.type === "model_request" && e.data.purpose === "compaction",
  );
  if (side === undefined) throw new Error("a summary answers a side request");
  const restored = await restoreDrafts(s, from.seq, to.seq);
  const stopped = s.append(
    draft.compacted(
      {
        from_seq: from.seq,
        to_seq: to.seq,
        from_event_id: from.event_id,
        to_event_id: to.event_id,
        summary_ref: s.store(text, "text/plain"),
        summary_request_event_id: side.event_id,
        trigger,
        ...(cause === undefined ? {} : { cause_event_id: cause }),
      },
      actor,
    ),
    ...restored,
  );
  if (stopped !== undefined) return { kind: "halt", halt: stopped };
  const hooked = await restoreHooks(s);
  return hooked === undefined
    ? { kind: "compacted" }
    : { kind: "halt", halt: hooked };
}

/**
 * before_compact gates the side request: a deny or failure is
 * compaction_failed{stage: hook}; a guide is recorded with its text, which the side request's
 * instruction line carries (Render v1). Every extension sees the state from before the first
 * call. For a request, an extension that already decided after it is not asked again.
 */
export async function beforeCompact(
  s: Session,
  request?: EventOf<"compaction_requested">,
): Promise<Compaction | undefined> {
  const window =
    request === undefined ? [] : s.events.filter((e) => e.seq > request.seq);
  const cause =
    request === undefined ? {} : { cause_event_id: request.event_id };
  const state = s.state();
  for (const ext of defining(s, "before_compact")) {
    if (recorded(window, ext.name, "before_compact") !== undefined) continue;
    const out = await run(ext, "before_compact", [state]);
    const made = compactDecision(ext.name, out);
    const denied =
      made.data.decision !== "proceed" && made.data.decision !== "guide";
    const stopped = denied
      ? s.append(
          made,
          draft.compactionFailed({
            stage: "hook",
            reason: "hook_denied",
            ...cause,
          }),
        )
      : s.append(made);
    if (stopped !== undefined) return { kind: "halt", halt: stopped };
    if (denied) return { kind: "failed" };
  }
  return undefined;
}

function compactDecision(
  ext: string,
  out: Outcome<"before_compact">,
): Extract<EventDraft, { type: "hook_decision" }> {
  const hook = "before_compact";
  if (out.kind === "failed")
    return decision(ext, hook, "failed", {}, out.reason);
  const v = out.value;
  switch (v.decision) {
    case "proceed":
      return decision(ext, hook, "proceed");
    case "guide":
      return decision(ext, hook, "guide", {}, v.text);
    case "deny":
      return decision(ext, hook, "deny", {}, v.reason);
    default:
      return assertNever(v);
  }
}

/**
 * L1: every result rendered in an earlier turn request, except the `keep` newest and those
 * already cleared, is replaced by the fixed placeholder.
 */
export function clear(
  s: Session,
  keep: number,
  reason: Reason,
): Halt | undefined {
  const events = s.events;
  const ctx = contextPolicy(s.fold.policy);
  const lastRequest = events.findLast(
    (e) => e.type === "model_request" && e.data.purpose !== "compaction",
  );
  const done = new Set(
    events.flatMap((e) =>
      e.type === "context_edited"
        ? e.data.edits.filter((x) => x.action === "clear").map((x) => x.call_id)
        : [],
    ),
  );
  const results = events.flatMap((e) =>
    e.type === "tool_result" && e.seq < (lastRequest?.seq ?? 0)
      ? [e.data.call_id]
      : [],
  );
  const excluded = new Set(ctx.clear_results.exclude_tools);
  const names = new Map(
    events.flatMap((e) =>
      e.type === "tool_call" ? [[e.data.call_id, e.data.name] as const] : [],
    ),
  );
  const ids = results
    .slice(0, Math.max(0, results.length - keep))
    .filter((id) => !done.has(id) && !excluded.has(names.get(id) ?? ""));
  if (ids.length === 0) return undefined;
  return s.append(
    draft.contextEdited({
      reason,
      edits: ids.map((call_id) => ({ call_id, action: "clear" })),
    }),
  );
}

/**
 * L2 range: from the first input after thread_started to the event before the shortest
 * step-boundary tail whose rendered estimate reaches keep_tail.
 */
function compactRange(
  s: Session,
): readonly [KnownEvent, KnownEvent] | undefined {
  const events = s.events;
  const from = events.find((e) => e.type === "user_input");
  if (from === undefined) return undefined;
  const keep = tokens(
    contextPolicy(s.fold.policy).compact.keep_tail,
    windowTokens(s),
  );
  const read = refReader(s.artifacts);
  const whole = render(events, read);
  if (!whole.ok) return undefined;
  for (const to of events.toReversed()) {
    if (to.seq < from.seq) return undefined;
    if (s.fold.boundaries[to.seq] !== true) continue;
    const head = render(
      events.filter((e) => e.seq <= to.seq),
      read,
    );
    if (!head.ok) return undefined;
    // ponytail: re-renders per candidate, O(n^2); keep per-line byte counts if sessions get long.
    const tail = whole.value.bytes.length - head.value.bytes.length;
    if (Math.ceil(tail / 4) >= keep) return [from, to];
  }
  return undefined;
}

/** W: the epoch model's context window minus reserve_tokens. */
export function windowTokens(s: Session): number {
  const model = s.fold.model;
  const listed = s.fold.policy?.models?.find(
    (m) => m.provider === model?.provider && m.name === model.name,
  );
  const adapter = model === undefined ? undefined : s.config.models(model);
  const window =
    listed?.context_window ?? adapter?.info.limits.context_window ?? 0;
  return window - contextPolicy(s.fold.policy).reserve_tokens;
}
