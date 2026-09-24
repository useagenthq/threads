import { assertNever, type EventOf, type KnownEvent } from "@threads/core/host";
import type { RunFacts } from "./facts";
import type { Chunk } from "./frame";
import {
  type Framed,
  isFramed,
  type ModelEvent,
  partId,
  type Shown,
  shownParts,
} from "./framed";

// A committed event as AI SDK UI message stream v1 chunks (spec/schema/ui/README.md, "Mapping").
// Pure: the event and the facts of the run's events before it, nothing else.

export function aiSdkChunks(e: KnownEvent, facts: RunFacts): readonly Chunk[] {
  return isFramed(e) ? framed(e, facts) : [];
}

function framed(e: Framed, facts: RunFacts): readonly Chunk[] {
  switch (e.type) {
    case "model_request":
    case "model_response":
    case "model_response_recovered":
    case "model_attempt_abandoned":
      return model(e, facts);
    case "approval_requested":
    case "approval_granted":
    case "approval_denied":
      return facts.proposed(e.data.call_id) ? [approval(e)] : [];
    case "tool_result":
    case "tool_result_late":
      return facts.proposed(e.data.call_id) ? [result(e)] : [];
    case "agent_spawned":
      return [subagent(e.data.child_thread_id, e.data.agent_name, "running")];
    case "agent_finished": {
      const spawned = facts.spawned(e.data.child_thread_id);
      return spawned === undefined
        ? []
        : [subagent(spawned.child, spawned.agent, e.data.status)];
    }
    case "retry_scheduled":
      return [
        {
          type: "data-status",
          id: "status",
          data: { status: "retry_wait", until: e.data.not_before },
        },
      ];
    default:
      return assertNever(e);
  }
}

/** A turn's model step: a compaction side request and its answer show nothing. */
function model(e: ModelEvent, facts: RunFacts): readonly Chunk[] {
  if (e.type === "model_request")
    return e.data.purpose === "compaction" ? [] : [{ type: "start-step" }];
  const requestId = e.data.request_event_id;
  if (!facts.isTurn(requestId)) return [];
  if (e.type === "model_attempt_abandoned")
    return [
      {
        type: "data-attempt",
        id: requestId,
        data: { status: "abandoned", reason: e.data.reason },
      },
      { type: "finish-step" },
    ];
  return [
    ...shownParts(e).flatMap((p) => part(requestId, p)),
    { type: "finish-step" },
  ];
}

/** One response part: text and reasoning open, fill and close a part; a call is one chunk. */
export function part(requestId: string, p: Shown): readonly Chunk[] {
  if (p.kind === "tool")
    return [
      {
        type: "tool-input-available",
        toolCallId: p.callId,
        toolName: p.name,
        input: p.input,
      },
    ];
  const id = partId(requestId, p.index);
  return [
    { type: `${p.kind}-start`, id },
    { type: `${p.kind}-delta`, id, delta: p.text },
    { type: `${p.kind}-end`, id },
  ];
}

function approval(
  e: EventOf<"approval_requested" | "approval_granted" | "approval_denied">,
): Chunk {
  if (e.type === "approval_requested")
    return {
      type: "tool-approval-request",
      approvalId: e.data.challenge_id,
      toolCallId: e.data.call_id,
    };
  return {
    type: "tool-approval-response",
    approvalId: e.data.challenge_id,
    approved: e.type === "approval_granted",
    ...(e.data.reason === undefined ? {} : { reason: e.data.reason }),
  };
}

function result(e: EventOf<"tool_result" | "tool_result_late">): Chunk {
  const toolCallId = e.data.call_id;
  const preview = e.data.preview;
  if (e.type === "tool_result" && e.data.origin === "deferred")
    return {
      type: "tool-output-available",
      toolCallId,
      output: preview,
      preliminary: true,
    };
  if (e.type === "tool_result" && e.data.origin === "denied")
    return { type: "tool-output-denied", toolCallId };
  if (e.data.is_error)
    return { type: "tool-output-error", toolCallId, errorText: preview };
  return { type: "tool-output-available", toolCallId, output: preview };
}

/** A legacy subagent as one data part, replaced in place by id as its status changes. */
export function subagent(child: string, agent: string, status: string): Chunk {
  return { type: "data-subagent", id: child, data: { agent, status } };
}
