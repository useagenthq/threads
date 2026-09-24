import {
  canonicalize,
  type EventOf,
  type KnownEvent,
} from "@threads/core/internal/feed";
import type { Attrs, SpanEvent } from "./span";

// Attributes by semantic conventions core v1.41.1 (spec/otel/README.md, "Attributes").

export const SEMCONV = "1.41.1";

const PROVIDERS: Readonly<Record<string, string>> = {
  bedrock: "aws.bedrock",
  vertex: "gcp.vertex_ai",
  azure: "azure.ai.openai",
};

/** A turn ending that is not a failure; every other reason is status ERROR. */
const OK_REASONS: ReadonlySet<string> = new Set([
  "end_turn",
  "cancelled",
  "handoff",
  "input_denied",
]);

type ModelRef = EventOf<"thread_started">["data"]["model"];
type Usage = EventOf<"model_response">["data"]["usage"];

export function turnAttrs(
  agent: string,
  threadId: string,
  runId: string | undefined,
  parentMissing: boolean,
): Attrs {
  return {
    "gen_ai.operation.name": "invoke_agent",
    "gen_ai.agent.name": agent,
    "gen_ai.conversation.id": threadId,
    "threads.run_id": runId,
    "threads.parent_missing": parentMissing || undefined,
  };
}

/** A turn_completed's reason and, for a failure, the status message. */
export function turnEnd(reason: string): {
  readonly attrs: Attrs;
  readonly status: string | undefined;
} {
  return {
    attrs: { "threads.turn.reason": reason },
    status: OK_REASONS.has(reason) ? undefined : reason,
  };
}

export function chatAttrs(
  model: ModelRef,
  request: EventOf<"model_request">,
): Attrs {
  return {
    "gen_ai.operation.name": "chat",
    "gen_ai.provider.name": PROVIDERS[model.provider] ?? model.provider,
    "gen_ai.request.model": model.name,
    "threads.model.attempt": request.data.attempt,
    "threads.model.purpose": request.data.purpose ?? "turn",
  };
}

/**
 * Cached input counts toward input_tokens. An absent cache field (no such billing category) is
 * 0; a null part (it never arrived) leaves the total out.
 */
export function usageAttrs(u: Usage): Attrs {
  const parts = [
    u.input_tokens,
    u.cache_read_tokens === undefined ? 0 : u.cache_read_tokens,
    u.cache_write_tokens === undefined ? 0 : u.cache_write_tokens,
  ];
  const total = parts.every((p) => p !== null)
    ? parts.reduce<number>((n, p) => n + (p ?? 0), 0)
    : undefined;
  return {
    "gen_ai.usage.input_tokens": total,
    "gen_ai.usage.cache_read.input_tokens": u.cache_read_tokens ?? undefined,
    "gen_ai.usage.cache_creation.input_tokens":
      u.cache_write_tokens ?? undefined,
    "gen_ai.usage.output_tokens": u.output_tokens ?? undefined,
    "gen_ai.usage.reasoning.output_tokens": u.reasoning_tokens ?? undefined,
  };
}

export function responseAttrs(
  response: EventOf<"model_response"> | EventOf<"model_response_recovered">,
  content: boolean,
): Attrs {
  const text = content
    ? response.data.content
        .flatMap((p) => (p.type === "text" ? [p.text] : []))
        .join("")
    : undefined;
  return {
    "gen_ai.response.finish_reasons": [response.data.stop_reason],
    ...usageAttrs(response.data.usage),
    "threads.model.output_text": text,
  };
}

export function toolAttrs(
  call: EventOf<"tool_call">,
  effectClass: string | undefined,
  resumed: boolean,
  content: boolean,
): Attrs {
  const args = content ? canonicalize(call.data.input) : undefined;
  return {
    "gen_ai.operation.name": "execute_tool",
    "gen_ai.tool.name": call.data.name,
    "gen_ai.tool.call.id": call.data.call_id,
    "gen_ai.tool.type": "function",
    "threads.tool.effect_class": effectClass,
    "threads.resumed": resumed || undefined,
    "gen_ai.tool.call.arguments": args?.ok === true ? args.value : undefined,
  };
}

/** A span event: the log event's type and time, `threads.seq` and its table attributes. */
export function spanEvent(e: KnownEvent, retryOf?: string): SpanEvent {
  return {
    time: e.time,
    name: e.type,
    attributes: {
      "threads.seq": e.seq,
      "threads.retry.of": retryOf,
      ...eventAttrs(e),
    },
  };
}

function eventAttrs(e: KnownEvent): Attrs {
  if (e.type === "permission_decision")
    return {
      "threads.permission.decision": e.data.decision,
      "threads.permission.source": e.data.source,
    };
  if (e.type === "effect_begin")
    return {
      "threads.call_id": e.data.call_id,
      "threads.effect.attempt": e.data.attempt,
    };
  if (e.type === "effect_unknown")
    return {
      "threads.call_id": e.data.call_id,
      "threads.effect.reason": e.data.reason,
    };
  if (e.type === "effect_resolved")
    return {
      "threads.call_id": e.data.call_id,
      "threads.effect.outcome": e.data.outcome,
      "threads.effect.by": e.data.by,
    };
  return otherAttrs(e);
}

function otherAttrs(e: KnownEvent): Attrs {
  if (
    e.type === "approval_requested" ||
    e.type === "approval_granted" ||
    e.type === "approval_denied" ||
    e.type === "effect_commit" ||
    e.type === "tool_result_late"
  )
    return { "threads.call_id": e.data.call_id };
  if (e.type === "retry_scheduled")
    return { "threads.retry.delay_ms": e.data.delay_ms };
  if (e.type === "compacted")
    return { "threads.compaction.trigger": e.data.trigger };
  if (e.type === "compaction_failed")
    return {
      "threads.compaction.stage": e.data.stage,
      "threads.compaction.reason": e.data.reason,
    };
  if (e.type === "budget_exceeded")
    return {
      "threads.budget.scope": e.data.scope,
      "threads.budget.limit": e.data.limit,
    };
  return {};
}
