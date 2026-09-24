import { z } from "zod";
import type { HookName } from "../hooks/types";
import type { EventType } from "../log";
import type { EnumOf } from "../log/zod-types";

// The two closed lists the eval runner reads (spec/schema/eval.v1.schema.json): which hooks
// always leave a hook_decision, and which events user code can append. Probe tests keep both
// honest: test/evals/hook-kinds.test.ts runs the real loop per hook, and
// test/evals/user-events.test.ts checks every event type and injected source is classified.

const RECORDED = [
  "before_input",
  "before_model",
  "before_tool",
  "before_tool_result",
  "before_compact",
  "before_model_switch",
  "on_stop",
  "permission_request",
  "after_model",
  "after_tool_batch",
  "after_compact",
  "session_start",
  "subagent_start",
  "subagent_stop",
] as const;
const OBSERVATION = [
  "after_tool",
  "after_model_switch",
  "permission_denied",
  "stop_failure",
  "session_end",
  "notification",
] as const;

/** A hook that appends a hook_decision on every call, proceed included. */
export const RecordedHook: EnumOf<typeof RECORDED> = z.enum(RECORDED).meta({
  id: "RecordedHook",
  description:
    "Always appends a hook_decision, proceed included, so a saved case records every call.",
});
/** A hook that appends a hook_decision only when it fails or annotates. */
export const ObservationHook: EnumOf<typeof OBSERVATION> = z
  .enum(OBSERVATION)
  .meta({
    id: "ObservationHook",
    description:
      "Appends a hook_decision only when it fails or annotates; a saved record is keyed by the call it belongs to.",
  });

export type HookKind = "recorded" | "observation";

/** Every hook's kind: the two lists above as one map, which the probe tests pin. */
export const HOOK_KINDS: { readonly [K in HookName]: HookKind } = {
  before_input: "recorded",
  before_model: "recorded",
  before_tool: "recorded",
  before_tool_result: "recorded",
  before_compact: "recorded",
  before_model_switch: "recorded",
  on_stop: "recorded",
  permission_request: "recorded",
  after_model: "recorded",
  after_tool_batch: "recorded",
  after_compact: "recorded",
  session_start: "recorded",
  subagent_start: "recorded",
  subagent_stop: "recorded",
  after_tool: "observation",
  after_model_switch: "observation",
  permission_denied: "observation",
  stop_failure: "observation",
  session_end: "observation",
  notification: "observation",
};

const SCRIPTABLE_EVENTS = ["hook_decision", "injected"] as const;
const FRAMEWORK_EVENTS = [
  "thread_started",
  "tools_changed",
  "user_input",
  "steer",
  "heartbeat",
  "model_request",
  "model_response",
  "model_response_recovered",
  "model_attempt_abandoned",
  "tool_call",
  "permission_decision",
  "approval_requested",
  "approval_granted",
  "approval_denied",
  "effect_begin",
  "effect_commit",
  "effect_unknown",
  "effect_resolved",
  "tool_result",
  "tool_result_late",
  "snapshot",
  "fork",
  "parked",
  "park_escalated",
  "resumed",
  "compacted",
  "cancel_requested",
  "cancelled",
  "stop_when_idle",
  "turn_completed",
  "schedule_fired",
  "schedule_skipped",
  "channel_delivery",
  "log_repaired",
  "settings_changed",
  "retry_scheduled",
  "budget_exceeded",
  "output_validated",
  "context_edited",
  "compaction_failed",
  "mode_changed",
  "permission_rule_added",
  "agent_spawned",
  "agent_finished",
  "handoff",
  "todos_updated",
  "team_task_created",
  "team_task_claimed",
  "team_task_updated",
  "team_message",
  "context_preflight_blocked",
  "compaction_requested",
  "team_opened",
  "member_started",
  "member_idle",
  "member_ended",
  "member_observed",
  "monitor_set",
  "wait_started",
  "wait_finished",
  "woken",
  "message_sent",
  "message_received",
  "mail_refused",
  "ask_closed",
  "operator_request",
  "operator_refused",
  "message_policy_decided",
  "answer_rejected",
  "tools_loaded",
] as const;
const SCRIPTABLE_SOURCES = ["hook", "memory", "knowledge"] as const;
const FRAMEWORK_SOURCES = [
  "skill",
  "attachment",
  "recovery",
  "todo",
  "mode",
  "output_style",
] as const;
const CHILD_THREAD_SOURCES = ["agent", "handoff"] as const;

/** Event types user code can make the loop append: replayed from extensions.json. */
export const ScriptableEvent: EnumOf<typeof SCRIPTABLE_EVENTS> = z
  .enum(SCRIPTABLE_EVENTS)
  .meta({ id: "ScriptableEvent" });
/** Event types only the loop appends, reproduced from the pin, the script and the results. */
export const FrameworkEvent: EnumOf<typeof FRAMEWORK_EVENTS> = z
  .enum(FRAMEWORK_EVENTS)
  .meta({ id: "FrameworkEvent" });
/** injected sources user code produces: hook text, recalled memory and knowledge. */
export const ScriptableSource: EnumOf<typeof SCRIPTABLE_SOURCES> = z
  .enum(SCRIPTABLE_SOURCES)
  .meta({ id: "ScriptableSource" });
/** injected sources the loop produces itself from the pin, the script and the results. */
export const FrameworkSource: EnumOf<typeof FRAMEWORK_SOURCES> = z
  .enum(FRAMEWORK_SOURCES)
  .meta({ id: "FrameworkSource" });
/** injected sources that occur only with child threads (not runnable offline). */
export const ChildThreadSource: EnumOf<typeof CHILD_THREAD_SOURCES> = z
  .enum(CHILD_THREAD_SOURCES)
  .meta({ id: "ChildThreadSource" });

/** What user code can append, as a closed list (spec lane 22, A.2). */
export const USER_EVENTS: {
  readonly scriptable: {
    readonly events: readonly EventType[];
    readonly sources: readonly string[];
  };
  readonly framework: {
    readonly events: readonly EventType[];
    readonly sources: readonly string[];
  };
  readonly child_thread: { readonly sources: readonly string[] };
} = {
  scriptable: { events: SCRIPTABLE_EVENTS, sources: SCRIPTABLE_SOURCES },
  framework: { events: FRAMEWORK_EVENTS, sources: FRAMEWORK_SOURCES },
  child_thread: { sources: CHILD_THREAD_SOURCES },
};
