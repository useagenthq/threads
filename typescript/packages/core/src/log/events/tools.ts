import { z } from "zod";
import { Actor, ActorWithPrincipal, PermissionMode } from "../common";
import { type EventSchema, event } from "../envelope";
import { CallId, ChallengeId, EventId } from "../ids";
import { JsonObject, Name, Sha256, TimeMs } from "../primitives";
import type { EnumOf, Opt, Strict } from "../zod-types";

// Tool calls and the decisions that gate them: permissions, hooks and approvals.

/** A schema-validated call, durable before authorization and dispatch. */
export const ToolCallData: Strict<{
  call_id: typeof CallId;
  name: typeof Name;
  input: typeof JsonObject;
  request_event_id: typeof EventId;
}> = z.strictObject({
  call_id: CallId,
  name: Name,
  input: JsonObject,
  request_event_id: EventId,
});
export const ToolCall: EventSchema<"tool_call", typeof ToolCallData, true> =
  event("tool_call", true, Actor, ToolCallData);

const PERMISSION_DECISIONS = ["allow", "deny", "ask"] as const;
const PERMISSION_SOURCES = [
  "policy",
  "hook",
  "default",
  "self_config_guard",
  "mode",
  "protected_path",
  "thread_rule",
] as const;
export const PermissionDecisionData: Strict<{
  call_id: typeof CallId;
  decision: EnumOf<typeof PERMISSION_DECISIONS>;
  source: EnumOf<typeof PERMISSION_SOURCES>;
  rule_id: Opt<z.ZodString>;
  mode: Opt<typeof PermissionMode>;
  reason: Opt<z.ZodString>;
}> = z.strictObject({
  call_id: CallId,
  decision: z.enum(PERMISSION_DECISIONS),
  source: z.enum(PERMISSION_SOURCES),
  rule_id: z.string().optional(),
  mode: PermissionMode.optional(),
  reason: z.string().optional(),
});
export const PermissionDecision: EventSchema<
  "permission_decision",
  typeof PermissionDecisionData,
  true
> = event("permission_decision", true, Actor, PermissionDecisionData);

const HOOKS = [
  "before_model",
  "after_model",
  "before_tool",
  "after_tool",
  "on_stop",
  "before_input",
  "after_tool_batch",
  "before_tool_result",
  "permission_request",
  "permission_denied",
  "before_compact",
  "after_compact",
  "stop_failure",
  "session_start",
  "session_end",
  "subagent_start",
  "subagent_stop",
  "before_model_switch",
  "after_model_switch",
  "notification",
] as const;
const HOOK_DECISIONS = [
  "proceed",
  "deny",
  "guide",
  "retry",
  "allow",
  "ask",
  "annotate",
  "stop",
  "continue",
  "failed",
  "redact",
] as const;
/** A policy hook's decision, durable before any work it gates. Replay never re-runs the hook. */
export const HookDecisionData: Strict<{
  extension: typeof Name;
  hook: EnumOf<typeof HOOKS>;
  decision: EnumOf<typeof HOOK_DECISIONS>;
  reason: Opt<z.ZodString>;
  request_event_id: Opt<typeof EventId>;
  call_id: Opt<typeof CallId>;
  input_event_id: Opt<typeof EventId>;
}> = z.strictObject({
  extension: Name,
  hook: z.enum(HOOKS),
  decision: z.enum(HOOK_DECISIONS),
  reason: z.string().optional(),
  request_event_id: EventId.optional(),
  call_id: CallId.optional(),
  input_event_id: EventId.optional(),
});
export const HookDecision: EventSchema<
  "hook_decision",
  typeof HookDecisionData,
  true
> = event("hook_decision", true, Actor, HookDecisionData);

/** A single-use challenge bound to one invocation. */
export const ApprovalRequestedData: Strict<{
  challenge_id: typeof ChallengeId;
  call_id: typeof CallId;
  args_hash: typeof Sha256;
  expires_at: typeof TimeMs;
}> = z.strictObject({
  challenge_id: ChallengeId,
  call_id: CallId,
  args_hash: Sha256,
  expires_at: TimeMs,
});
export const ApprovalRequested: EventSchema<
  "approval_requested",
  typeof ApprovalRequestedData,
  true
> = event("approval_requested", true, Actor, ApprovalRequestedData);

export const ApprovalBinding: Strict<{
  challenge_id: typeof ChallengeId;
  call_id: typeof CallId;
  args_hash: typeof Sha256;
  reason: Opt<z.ZodString>;
}> = z
  .strictObject({
    challenge_id: ChallengeId,
    call_id: CallId,
    args_hash: Sha256,
    reason: z.string().optional(),
  })
  .meta({ id: "ApprovalBinding" });
/** actor.principal is the approver. */
export const ApprovalGranted: EventSchema<
  "approval_granted",
  typeof ApprovalBinding,
  true,
  typeof ActorWithPrincipal
> = event("approval_granted", true, ActorWithPrincipal, ApprovalBinding);
export const ApprovalDenied: EventSchema<
  "approval_denied",
  typeof ApprovalBinding,
  true,
  typeof ActorWithPrincipal
> = event("approval_denied", true, ActorWithPrincipal, ApprovalBinding);
