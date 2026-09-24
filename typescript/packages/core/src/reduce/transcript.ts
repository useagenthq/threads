import { assertNever } from "../assert-never";
import type { KnownEvent } from "../log";
import {
  assistantParts,
  type View,
  type VisibleEvent,
  view,
  walk,
} from "../render/view";

export type TranscriptEntry = {
  readonly role: "user" | "context" | "assistant" | "tool" | "summary";
  readonly event_id: string;
};

/**
 * ReducedState.transcript: `{role, event_id}` for each conversational line the next request
 * renders, in render order (spec/conformance/README.md; reference: render.py `transcript`).
 */
export function transcript(
  events: readonly KnownEvent[],
): readonly TranscriptEntry[] {
  // A team log has no thread_started: it never renders.
  if (!events.some((e) => e.type === "thread_started")) return [];
  const v = view(events);
  const out: TranscriptEntry[] = [];
  for (const entry of walk(v, events)) {
    if (entry.kind === "summary") {
      out.push({ role: "summary", event_id: entry.compacted.event_id });
      continue;
    }
    const role = roleOf(v, entry.event);
    if (role !== undefined) out.push({ role, event_id: entry.event.event_id });
  }
  return out;
}

function roleOf(v: View, e: VisibleEvent): TranscriptEntry["role"] | undefined {
  switch (e.type) {
    case "user_input":
    case "steer":
      return v.denied.has(e.event_id) ? undefined : "user";
    case "injected":
    case "heartbeat":
    case "message_received":
      return "context";
    case "model_response":
    case "model_response_recovered":
      return assistantParts(v, e).length > 0 ? "assistant" : undefined;
    case "tool_result":
    case "tool_result_late":
      return "tool";
    case "tools_changed":
      return undefined;
    default:
      return assertNever(e);
  }
}
