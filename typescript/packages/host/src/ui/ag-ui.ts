import {
  assertNever,
  canonicalize,
  type EventOf,
  type KnownEvent,
} from "@threads/core/host";
import type { RunFacts, Spawned } from "./facts";
import type { Chunk } from "./frame";
import {
  type Framed,
  isFramed,
  type ModelEvent,
  partId,
  type Shown,
  shownParts,
} from "./framed";

// A committed event as AG-UI 1.0 events (spec/schema/ui/README.md, "Mapping"). Pure: the event
// and the facts of the run's events before it, nothing else.

export const MODEL_STEP: Chunk = { type: "STEP_STARTED", stepName: "model" };
const STEP_DONE: Chunk = { type: "STEP_FINISHED", stepName: "model" };

export function agUiEvents(e: KnownEvent, facts: RunFacts): readonly Chunk[] {
  return isFramed(e) ? framed(e, facts) : [];
}

function framed(e: Framed, facts: RunFacts): readonly Chunk[] {
  switch (e.type) {
    case "model_request":
    case "model_response":
    case "model_response_recovered":
    case "model_attempt_abandoned":
      return model(e, facts);
    case "tool_result":
      return e.data.origin === "deferred" || !facts.proposed(e.data.call_id)
        ? []
        : [result(e)];
    case "tool_result_late":
      return facts.proposed(e.data.call_id) ? [result(e)] : [];
    case "agent_spawned":
      return [
        started({
          child: e.data.child_thread_id,
          agent: e.data.agent_name,
          callId: e.data.call_id,
        }),
      ];
    case "agent_finished":
      return finished(e, facts);
    case "retry_scheduled":
      return [
        {
          type: "CUSTOM",
          name: "threads.retry_wait",
          value: { until: e.data.not_before },
        },
      ];
    case "approval_requested":
    case "approval_granted":
    case "approval_denied":
      // An approval becomes an interrupt when the run parks on it.
      return [];
    default:
      return assertNever(e);
  }
}

/** A turn's model step: a compaction side request and its answer show nothing. */
function model(e: ModelEvent, facts: RunFacts): readonly Chunk[] {
  if (e.type === "model_request")
    return e.data.purpose === "compaction" ? [] : [MODEL_STEP];
  const requestId = e.data.request_event_id;
  if (!facts.isTurn(requestId)) return [];
  if (e.type === "model_attempt_abandoned") return [abandoned(e), STEP_DONE];
  return [...shownParts(e).flatMap((p) => part(requestId, p)), STEP_DONE];
}

function finished(
  e: EventOf<"agent_finished">,
  facts: RunFacts,
): readonly Chunk[] {
  const spawned = facts.spawned(e.data.child_thread_id);
  if (spawned === undefined) return [];
  return [
    e.data.status === "completed"
      ? {
          type: "SUBAGENT_FINISHED",
          subagentRunId: spawned.child,
          outcome: { type: "success" },
        }
      : {
          type: "SUBAGENT_ERROR",
          subagentRunId: spawned.child,
          message: `subagent ${spawned.agent} ended: ${e.data.status}`,
          code: e.data.status,
        },
  ];
}

/** One response part: text and reasoning are messages of their own; calls join the request's. */
export function part(requestId: string, p: Shown): readonly Chunk[] {
  if (p.kind === "tool") {
    const args = canonicalize(p.input);
    if (!args.ok) throw new Error(`a logged tool input is JSON: ${args.error}`);
    return [
      {
        type: "TOOL_CALL_START",
        toolCallId: p.callId,
        toolCallName: p.name,
        parentMessageId: requestId,
      },
      { type: "TOOL_CALL_ARGS", toolCallId: p.callId, delta: args.value },
      { type: "TOOL_CALL_END", toolCallId: p.callId },
    ];
  }
  const messageId = partId(requestId, p.index);
  if (p.kind === "text")
    return [
      { type: "TEXT_MESSAGE_START", messageId, role: "assistant" },
      { type: "TEXT_MESSAGE_CONTENT", messageId, delta: p.text },
      { type: "TEXT_MESSAGE_END", messageId },
    ];
  return [
    { type: "REASONING_START", messageId },
    { type: "REASONING_MESSAGE_START", messageId, role: "reasoning" },
    { type: "REASONING_MESSAGE_CONTENT", messageId, delta: p.text },
    { type: "REASONING_MESSAGE_END", messageId },
    { type: "REASONING_END", messageId },
  ];
}

function abandoned(e: EventOf<"model_attempt_abandoned">): Chunk {
  return {
    type: "CUSTOM",
    name: "threads.attempt_abandoned",
    value: { requestId: e.data.request_event_id, reason: e.data.reason },
  };
}

function result(e: EventOf<"tool_result" | "tool_result_late">): Chunk {
  return {
    type: "TOOL_CALL_RESULT",
    messageId: e.event_id,
    toolCallId: e.data.call_id,
    content: e.data.preview,
    role: "tool",
  };
}

/** A running legacy subagent: its start event, also sent in a replay's open-state preamble. */
export function started(s: Spawned): Chunk {
  return {
    type: "SUBAGENT_STARTED",
    subagentRunId: s.child,
    name: s.agent,
    parentToolCallId: s.callId,
  };
}
