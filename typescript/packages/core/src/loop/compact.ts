import { assertNever } from "../assert-never";
import type { Outcome } from "../hooks/invoke";
import type { KnownEvent } from "../log";
import { refReader, render } from "../render";
import type { EventDraft } from "../store";
import { attempt } from "./attempt";
import { draft } from "./drafts";
import { decision, defining, run } from "./hooks";
import { contextPolicy, tokens } from "./policy";
import { restore } from "./restore";
import type { Session } from "./session";
import { stepEvents } from "./turn";
import type { Halt } from "./types";

// (clear old results) and L2 (summary compaction, then L3 restore), run on a
// threshold by the ladder (ladder.ts) or reactively by L4 and L5.

export type Compaction =
  | { readonly kind: "compacted" }
  | { readonly kind: "failed" }
  /** The summary request's response held a registered secret: the turn has ended. */
  | { readonly kind: "ended" }
  | { readonly kind: "halt"; readonly halt: Halt };

type Reason = "threshold" | "compaction_fallback";

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
  let got = await attempt(s, "compaction", 1);
  if (got.kind === "rejected" && got.rejection.reason === "prompt_too_long") {
    // The side request itself is too long: clear everything clearable and retry it once.
    const fallback = clear(s, 0, "compaction_fallback");
    if (fallback !== undefined) return { kind: "halt", halt: fallback };
    got = await attempt(s, "compaction", 2);
  }
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
      return { kind: "ended" };
    case "broken":
    case "unsupported":
    case "budget":
      return failed(s, "model_error");
    default:
      return assertNever(got);
  }
}

function failed(
  s: Session,
  reason:
    | "still_over_threshold"
    | "empty_summary"
    | "prompt_too_long"
    | "model_error",
): Compaction {
  const side = s.events.findLast(
    (e) => e.type === "model_request" && e.data.purpose === "compaction",
  );
  const stopped = s.append(
    draft.compactionFailed({
      stage: "summary",
      reason,
      ...(side === undefined || reason === "still_over_threshold"
        ? {}
        : { request_event_id: side.event_id }),
    }),
  );
  return stopped === undefined
    ? { kind: "failed" }
    : { kind: "halt", halt: stopped };
}

async function summarized(
  s: Session,
  range: readonly [KnownEvent, KnownEvent],
  text: string,
  trigger: "reactive" | "threshold",
): Promise<Compaction> {
  const [from, to] = range;
  const side = s.events.findLast((e) => e.type === "model_request");
  if (side === undefined) throw new Error("a summary answers a side request");
  const stopped = s.append(
    draft.compacted({
      from_seq: from.seq,
      to_seq: to.seq,
      from_event_id: from.event_id,
      to_event_id: to.event_id,
      summary_ref: s.store(text, "text/plain"),
      summary_request_event_id: side.event_id,
      trigger,
    }),
  );
  if (stopped !== undefined) return { kind: "halt", halt: stopped };
  const done = s.events.at(-1);
  if (done?.type !== "compacted")
    throw new Error("compacted was just appended");
  const restored = await restore(s, done);
  return restored === undefined
    ? { kind: "compacted" }
    : { kind: "halt", halt: restored };
}

/**
 * before_compact gates the side request: a deny or failure is
 * compaction_failed{stage: hook}; a guide is recorded with its text, which the side request's
 * instruction line carries (Render v1).
 */
async function beforeCompact(s: Session): Promise<Compaction | undefined> {
  for (const ext of defining(s, "before_compact")) {
    const out = await run(ext, "before_compact", [s.state()]);
    const recorded = compactDecision(ext.name, out);
    const denied =
      recorded.data.decision !== "proceed" &&
      recorded.data.decision !== "guide";
    const stopped = denied
      ? s.append(
          recorded,
          draft.compactionFailed({ stage: "hook", reason: "hook_denied" }),
        )
      : s.append(recorded);
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
