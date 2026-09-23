import { assertNever } from "../assert-never";
import type { KnownEvent } from "../log";
import { refReader, render } from "../render";
import { attempt } from "./attempt";
import { draft } from "./drafts";
import { contextPolicy, tokens } from "./policy";
import type { Session } from "./session";
import type { Halt } from "./types";

// (clear old results) and L2 (summary compaction), run by L5 on a prompt_too_long
// rejection. Threshold-driven runs, L3 restore and L4 preflight need the token estimate and
// come with long-session support.

export type Compaction =
  | { readonly kind: "compacted" }
  | { readonly kind: "failed" }
  | { readonly kind: "halt"; readonly halt: Halt };

type Reason = "threshold" | "compaction_fallback";

export async function compact(
  s: Session,
  trigger: "reactive" | "threshold",
): Promise<Compaction> {
  const ctx = contextPolicy(s.fold.policy);
  const cleared = clear(s, ctx.clear_results.keep_recent, "threshold");
  if (cleared !== undefined) return { kind: "halt", halt: cleared };
  const range = compactRange(s);
  if (range === undefined) return failed(s, "still_over_threshold");
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
    case "broken":
    case "unsupported":
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

function summarized(
  s: Session,
  range: readonly [KnownEvent, KnownEvent],
  text: string,
  trigger: "reactive" | "threshold",
): Compaction {
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
  return stopped === undefined
    ? { kind: "compacted" }
    : { kind: "halt", halt: stopped };
}

/**
 * L1: every result rendered in an earlier turn request, except the `keep` newest and those
 * already cleared, is replaced by the fixed placeholder.
 */
function clear(s: Session, keep: number, reason: Reason): Halt | undefined {
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
function windowTokens(s: Session): number {
  const model = s.fold.model;
  const listed = s.fold.policy?.models?.find(
    (m) => m.provider === model?.provider && m.name === model.name,
  );
  const adapter = model === undefined ? undefined : s.config.models(model);
  const window =
    listed?.context_window ?? adapter?.info.limits.context_window ?? 0;
  return window - contextPolicy(s.fold.policy).reserve_tokens;
}
