import { assertNever } from "../assert-never";
import type { EventOf } from "../fold/state";
import type { KnownEvent } from "../log";

/** The events that render a conversational line (Render v1 table, spec/schema/README.md). */
export type VisibleEvent = EventOf<
  | "user_input"
  | "steer"
  | "injected"
  | "heartbeat"
  | "model_response"
  | "model_response_recovered"
  | "tools_changed"
  | "tool_result"
  | "tool_result_late"
>;
export type Compacted = EventOf<"compacted">;

/** One render step: a visible event, or a compacted range's summary in place of the range. */
export type Entry =
  | { readonly kind: "event"; readonly event: VisibleEvent }
  | { readonly kind: "summary"; readonly compacted: Compacted };

/** What Render v1 drops or rewrites, from one pass over the events before the request. */
export type View = {
  readonly denied: ReadonlySet<string>;
  readonly side: ReadonlySet<string>;
  /** Opaque parts of responses before this seq are omitted (reasoning_carryover omit_prior). */
  readonly cut: number;
  /** Outermost compacted ranges: a compacted event inside a later range is dropped with it. */
  readonly ranges: readonly Compacted[];
  /** Calls no model response proposed (a host's channel_send reply): their results render nothing. */
  readonly hostCalls: ReadonlySet<string>;
};

function covers(c: Compacted, seq: number): boolean {
  return c.data.from_seq <= seq && seq <= c.data.to_seq;
}

export function view(events: readonly KnownEvent[]): View {
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
    hostCalls: hostCalls(events),
  };
}

function hostCalls(events: readonly KnownEvent[]): ReadonlySet<string> {
  const proposed = new Set(
    events.flatMap((e) =>
      e.type === "model_response" || e.type === "model_response_recovered"
        ? e.data.content.flatMap((p) =>
            p.type === "tool_use" ? [p.call_id] : [],
          )
        : [],
    ),
  );
  return new Set(
    events.flatMap((e) =>
      e.type === "tool_call" && !proposed.has(e.data.call_id)
        ? [e.data.call_id]
        : [],
    ),
  );
}

/** The input a before_input hook denied (or failed on); it renders nothing. */
function deniedInput(e: KnownEvent): readonly string[] {
  if (e.type !== "hook_decision" || e.data.hook !== "before_input") return [];
  const { decision, input_event_id: input } = e.data;
  const denies = decision === "deny" || decision === "failed";
  return denies && input !== undefined ? [input] : [];
}

/**
 * The render steps in order. A compacted range yields its summary at the range's first event,
 * then the range's last `tools_changed`, so loaded tools survive (reference: render.py `walk`).
 */
export function* walk(
  v: View,
  events: readonly KnownEvent[],
): Generator<Entry, void, undefined> {
  for (const e of events) {
    const range = v.ranges.find((c) => covers(c, e.seq));
    if (range === undefined) {
      const shown = hidden(v, e) ? undefined : visible(e);
      if (shown !== undefined) yield { kind: "event", event: shown };
    } else if (e.seq === range.data.from_seq) {
      yield { kind: "summary", compacted: range };
      const tools = events.findLast(
        (x) => x.type === "tools_changed" && covers(range, x.seq),
      );
      if (tools?.type === "tools_changed")
        yield { kind: "event", event: tools };
    }
  }
}

/** A response's parts after omit_prior; a compaction side response renders none. */
export function assistantParts(
  v: View,
  e: EventOf<"model_response" | "model_response_recovered">,
): EventOf<"model_response">["data"]["content"] {
  if (v.side.has(e.data.request_event_id)) return [];
  const old = e.seq < v.cut;
  return e.data.content.filter(
    (part) =>
      !(old && (part.type === "reasoning" || part.type === "hosted_tool")),
  );
}

function hidden(v: View, e: KnownEvent): boolean {
  return (
    (e.type === "tool_result" || e.type === "tool_result_late") &&
    v.hostCalls.has(e.data.call_id)
  );
}

function visible(e: KnownEvent): VisibleEvent | undefined {
  switch (e.type) {
    case "user_input":
    case "steer":
    case "injected":
    case "heartbeat":
    case "model_response":
    case "model_response_recovered":
    case "tools_changed":
    case "tool_result":
    case "tool_result_late":
      return e;
    case "thread_started":
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
