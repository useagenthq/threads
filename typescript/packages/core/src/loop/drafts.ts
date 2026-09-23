import { CallId } from "../log";
import type { EventDraft } from "../store";

// Typed drafts for the events the loop appends. Every one here is critical (schema-pinned).

type DraftOf<T extends EventDraft["type"]> = Extract<EventDraft, { type: T }>;
type Data<T extends EventDraft["type"]> = DraftOf<T>["data"];
type Actor = { readonly kind: "host" | "model" | "tool" | "recovery" };

export const HOST: { readonly kind: "host" } = { kind: "host" };
export const RECOVERY: { readonly kind: "recovery" } = { kind: "recovery" };
export const MODEL: { readonly kind: "model" } = { kind: "model" };
export const TOOL: { readonly kind: "tool" } = { kind: "tool" };

const base = { type_version: 1, critical: true } as const;

export const draft = {
  modelRequest: (d: Data<"model_request">): EventDraft => ({
    ...base,
    type: "model_request",
    actor: HOST,
    data: d,
  }),
  modelResponse: (d: Data<"model_response">): EventDraft => ({
    ...base,
    type: "model_response",
    actor: MODEL,
    data: d,
  }),
  recovered: (d: Data<"model_response_recovered">): EventDraft => ({
    ...base,
    type: "model_response_recovered",
    actor: RECOVERY,
    data: d,
  }),
  abandoned: (
    d: Data<"model_attempt_abandoned">,
    actor: Actor = HOST,
  ): EventDraft => ({
    ...base,
    type: "model_attempt_abandoned",
    actor,
    data: d,
  }),
  retryScheduled: (d: Data<"retry_scheduled">): EventDraft => ({
    ...base,
    type: "retry_scheduled",
    actor: HOST,
    data: d,
  }),
  settingsChanged: (d: Data<"settings_changed">): EventDraft => ({
    ...base,
    type: "settings_changed",
    actor: HOST,
    data: d,
  }),
  toolCall: (d: Data<"tool_call">): EventDraft => ({
    ...base,
    type: "tool_call",
    actor: HOST,
    data: d,
  }),
  permission: (d: Data<"permission_decision">): EventDraft => ({
    ...base,
    type: "permission_decision",
    actor: HOST,
    data: d,
  }),
  approvalRequested: (d: Data<"approval_requested">): EventDraft => ({
    ...base,
    type: "approval_requested",
    actor: HOST,
    data: d,
  }),
  effectBegin: (d: Data<"effect_begin">): EventDraft => ({
    ...base,
    type: "effect_begin",
    actor: HOST,
    data: d,
  }),
  effectCommit: (d: Data<"effect_commit">): EventDraft => ({
    ...base,
    type: "effect_commit",
    actor: HOST,
    data: d,
  }),
  effectUnknown: (
    d: Data<"effect_unknown">,
    actor: Actor = HOST,
  ): EventDraft => ({
    ...base,
    type: "effect_unknown",
    actor,
    data: d,
  }),
  effectResolved: (
    d: Data<"effect_resolved">,
    actor: Actor = RECOVERY,
  ): EventDraft => ({ ...base, type: "effect_resolved", actor, data: d }),
  toolResult: (
    d: Omit<Data<"tool_result">, "completeness">,
    actor: Actor = TOOL,
  ): EventDraft => ({
    ...base,
    type: "tool_result",
    actor,
    data: { completeness: "complete", ...d },
  }),
  parked: (d: Data<"parked">, actor: Actor = HOST): EventDraft => ({
    ...base,
    type: "parked",
    actor,
    data: d,
  }),
  cancelled: (d: Data<"cancelled">): EventDraft => ({
    ...base,
    type: "cancelled",
    actor: HOST,
    data: d,
  }),
  turnCompleted: (
    reason: Data<"turn_completed">["reason"],
    code?: NonNullable<Data<"turn_completed">["code"]>,
  ): EventDraft => ({
    ...base,
    type: "turn_completed",
    actor: HOST,
    data: code === undefined ? { reason } : { reason, code },
  }),
  injected: (d: Data<"injected">): EventDraft => ({
    ...base,
    type: "injected",
    actor: HOST,
    data: d,
  }),
  outputValidated: (d: Data<"output_validated">): EventDraft => ({
    ...base,
    type: "output_validated",
    actor: HOST,
    data: d,
  }),
  contextEdited: (d: Data<"context_edited">): EventDraft => ({
    ...base,
    type: "context_edited",
    actor: HOST,
    data: d,
  }),
  compacted: (d: Data<"compacted">): EventDraft => ({
    ...base,
    type: "compacted",
    actor: HOST,
    data: d,
  }),
  compactionFailed: (d: Data<"compaction_failed">): EventDraft => ({
    ...base,
    type: "compaction_failed",
    actor: HOST,
    data: d,
  }),
  preflightBlocked: (d: Data<"context_preflight_blocked">): EventDraft => ({
    ...base,
    type: "context_preflight_blocked",
    actor: HOST,
    data: d,
  }),
  heartbeat: (running: readonly string[]): EventDraft => ({
    ...base,
    type: "heartbeat",
    actor: HOST,
    data: { running_call_ids: running.map((id) => CallId.parse(id)) },
  }),
  budgetExceeded: (d: Data<"budget_exceeded">): EventDraft => ({
    ...base,
    type: "budget_exceeded",
    actor: HOST,
    data: d,
  }),
};
