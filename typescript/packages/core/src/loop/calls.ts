import { isDeferred } from "../fold/state";
import type { EventDraft } from "../store";
import { AGENT_TOOLS, entry, MEMBER_TOOLS } from "../tools/catalog";
import { FINAL_OUTPUT } from "../tools/loop-tools";
import { authorize } from "./authorize";
import { draft } from "./drafts";
import { parseErrors } from "./schema";
import type { Session } from "./session";
import { queryTooLong, searchPinned } from "./tool-search";
import { cancelRequested, owedCalls, type ToolUse, toolSpec } from "./turn";
import type { Halt } from "./types";

// A response's tool calls become durable before anything runs: tool_call (the parsed args),
// then its authorization, or an error result for a call that fails before any effect.

/**
 * Records each part the log lacks, and authorizes each recorded call still pending without a
 * decision, in part order: a run resumed mid-recording finishes it as an uninterrupted run would.
 * After a cancel nothing new is authorized: the cancellation step closes the rest.
 */
export async function recordCalls(s: Session): Promise<Halt | undefined> {
  const owed = owedCalls(s.events, s.fold);
  if (owed === undefined) return undefined;
  const requestId = owed.response.data.request_event_id;
  for (const part of owed.parts) {
    if (cancelRequested(s.events, s.fold) !== undefined) return undefined;
    const stopped =
      part.kind === "undecided"
        ? await authorize(s, part.call)
        : await recordCall(s, part.use, requestId);
    if (stopped !== undefined) return stopped;
  }
  return undefined;
}

/** Behind a cancel: each part not yet recorded gets its tool_call and not_executed result. */
export function closeUnrecorded(s: Session): Halt | undefined {
  const owed = owedCalls(s.events, s.fold);
  if (owed === undefined) return undefined;
  const requestId = owed.response.data.request_event_id;
  const closed = owed.parts.flatMap((part) =>
    part.kind === "unrecorded"
      ? refused(part.use, requestId, "not executed: cancelled")
      : [],
  );
  return closed.length === 0 ? undefined : s.append(...closed);
}

/** A call that never runs: its tool_call and its not_executed result, appended together. */
function refused(
  use: ToolUse,
  requestEventId: string,
  preview: string,
): readonly EventDraft[] {
  const { call_id, name, input } = use;
  return [
    draft.toolCall({ call_id, name, input, request_event_id: requestEventId }),
    draft.toolResult(
      { call_id, is_error: true, origin: "not_executed", preview },
      { kind: "host" },
    ),
  ];
}

async function recordCall(
  s: Session,
  use: ToolUse,
  requestEventId: string,
): Promise<Halt | undefined> {
  // One batch: a crash between the call and its refusal must never leave it runnable.
  const failure = preEffectFailure(s, use);
  if (failure !== undefined)
    return s.append(...refused(use, requestEventId, failure));
  const { call_id, name, input } = use;
  const stopped = s.append(
    draft.toolCall({ call_id, name, input, request_event_id: requestEventId }),
  );
  if (stopped !== undefined) return stopped;
  const call = s.events.at(-1);
  if (call?.type !== "tool_call")
    throw new Error("the tool_call was just appended");
  return authorize(s, call);
}

/** Unknown, deferred or invalid calls fail before any effect, with a reason the model sees. */
function preEffectFailure(s: Session, use: ToolUse): string | undefined {
  const spec = toolSpec(s.fold, use.name);
  if (spec === undefined) return `unknown tool: ${use.name}`;
  // A final_output candidate is checked once, as output_validated.
  if (use.name === FINAL_OUTPUT) return undefined;
  if (isDeferred(s.fold, spec))
    return `tool_not_loaded: ${use.name}; find it with tool_search first`;
  const search = use.name === "tool_search" && searchPinned(s.fold);
  const framework =
    search ||
    (AGENT_TOOLS.has(use.name) && use.name !== "tool_search") ||
    (s.config.team !== undefined && MEMBER_TOOLS.has(use.name));
  const input = framework
    ? entry(use.name).input
    : s.config.tools.get(use.name)?.input;
  // Arguments parse with the tool's own schema; a tool without one fails closed.
  if (input === undefined) return `no implementation for ${use.name}`;
  const errors = parseErrors(input, use.input);
  if (errors !== undefined) return `invalid input: ${errors}`;
  return search ? queryTooLong(use.input) : undefined;
}
