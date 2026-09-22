import { assertNever } from "../assert-never";
import type { EventOf } from "../fold/state";
import type { KnownEvent } from "../log";

export type TranscriptEntry = {
  readonly role: "user" | "context" | "assistant" | "tool" | "summary";
  readonly event_id: string;
};

type Compacted = EventOf<"compacted">;

/** What Render v1 drops or rewrites, from one pass over the events before the request. */
type View = {
  readonly denied: ReadonlySet<string>;
  readonly side: ReadonlySet<string>;
  /** Opaque parts of responses before this seq are omitted (reasoning_carryover omit_prior). */
  readonly cut: number;
  /** Outermost compacted ranges: a compacted event inside a later range is dropped with it. */
  readonly ranges: readonly Compacted[];
};

function covers(c: Compacted, seq: number): boolean {
  return c.data.from_seq <= seq && seq <= c.data.to_seq;
}

function view(events: readonly KnownEvent[]): View {
  const compacted = events.flatMap((e) => (e.type === "compacted" ? [e] : []));
  return {
    denied: new Set(events.flatMap(deniedInput)),
    side: new Set(
      events.flatMap((e) =>
        e.type === "model_request" && e.data.purpose === "compaction"
          ? [e.event_id]
          : [],
      ),
    ),
    cut: events.reduce(
      (cut, e) =>
        e.type === "settings_changed" &&
        e.data.settings.reasoning_carryover === "omit_prior"
          ? e.seq
          : cut,
      0,
    ),
    ranges: compacted.filter(
      (c) => !compacted.some((outer) => covers(outer, c.seq)),
    ),
  };
}

/** The input a before_input hook denied (or failed on); it renders nothing. */
function deniedInput(e: KnownEvent): readonly string[] {
  if (e.type !== "hook_decision" || e.data.hook !== "before_input") return [];
  const { decision, input_event_id: input } = e.data;
  const denies = decision === "deny" || decision === "failed";
  return denies && input !== undefined ? [input] : [];
}

/**
 * ReducedState.transcript: `{role, event_id}` for each conversational line the next request
 * renders, in render order (spec/conformance/README.md; reference: render.py `transcript`).
 */
export function transcript(
  events: readonly KnownEvent[],
): readonly TranscriptEntry[] {
  const v = view(events);
  const out: TranscriptEntry[] = [];
  for (const e of events) {
    const range = v.ranges.find((c) => covers(c, e.seq));
    if (range === undefined) {
      const role = roleOf(v, e);
      if (role !== undefined) out.push({ role, event_id: e.event_id });
    } else if (e.seq === range.data.from_seq) {
      out.push({ role: "summary", event_id: range.event_id });
    }
  }
  return out;
}

function roleOf(v: View, e: KnownEvent): TranscriptEntry["role"] | undefined {
  switch (e.type) {
    case "user_input":
    case "steer":
      return v.denied.has(e.event_id) ? undefined : "user";
    case "injected":
    case "heartbeat":
      return "context";
    case "model_response":
    case "model_response_recovered":
      return rendersAssistant(v, e) ? "assistant" : undefined;
    case "tool_result":
    case "tool_result_late":
      return "tool";
    case "thread_started":
    case "tools_changed":
    case "model_request":
    case "model_attempt_abandoned":
    case "tool_call":
    case "permission_decision":
    case "hook_decision":
    case "approval_requested":
    case "approval_granted":
    case "approval_denied":
    case "effect_begin":
    case "effect_commit":
    case "effect_unknown":
    case "effect_resolved":
    case "snapshot":
    case "fork":
    case "parked":
    case "park_escalated":
    case "resumed":
    case "compacted":
    case "cancel_requested":
    case "cancelled":
    case "stop_when_idle":
    case "turn_completed":
    case "schedule_fired":
    case "schedule_skipped":
    case "channel_delivery":
    case "log_repaired":
    case "settings_changed":
    case "retry_scheduled":
    case "budget_exceeded":
    case "output_validated":
    case "context_edited":
    case "compaction_failed":
    case "mode_changed":
    case "permission_rule_added":
    case "agent_spawned":
    case "agent_finished":
    case "handoff":
    case "todos_updated":
    case "team_task_created":
    case "team_task_claimed":
    case "team_task_updated":
    case "team_message":
    case "context_preflight_blocked":
      return undefined;
    default:
      return assertNever(e);
  }
}

/** A compaction side response renders nothing, and so does one with no parts left. */
function rendersAssistant(
  v: View,
  e: EventOf<"model_response" | "model_response_recovered">,
): boolean {
  if (v.side.has(e.data.request_event_id)) return false;
  const old = e.seq < v.cut;
  return e.data.content.some(
    (part) =>
      !(old && (part.type === "reasoning" || part.type === "hosted_tool")),
  );
}
