import { z } from "zod";
import { ActorWithPrincipal, ArtifactRef, PermissionMode } from "../common";
import { type EventDef, event, eventWithActor } from "../envelope";
import { CallId, ChallengeId, EventId } from "../ids";
import { JsonObject, Name, Sha256, TimeMs } from "../primitives";
import type { Arr, EnumOf, Opt, Strict } from "../zod-types";

// Tool calls and the decisions that gate them: permissions, hooks and approvals.

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
export const ToolCall: EventDef<"tool_call", typeof ToolCallData, true> = event(
  {
    type: "tool_call",
    critical: true,
    description:
      "A schema-validated call, durable before authorization and dispatch. input is the parsed args exactly. Derived, not stored: args_hash = sha256(RFC 8785 of input); effect_class and dedup_window_ms from the pinned ToolSpec named name; effect key = <this event's branch_id>:<call_id>. request_event_id lets a streamed call be recorded before its model_response.",
    data: ToolCallData,
  },
);

export const LoadedTool: Strict<{
  name: typeof Name;
  spec_ref: typeof ArtifactRef;
}> = z
  .strictObject({ name: Name, spec_ref: ArtifactRef })
  .meta({ id: "LoadedTool" });

export const ToolsLoadedData: Strict<{
  call_id: typeof CallId;
  tools: Arr<typeof LoadedTool>;
}> = z.strictObject({
  call_id: CallId,
  tools: z.array(LoadedTool).min(1),
});
export const ToolsLoaded: EventDef<
  "tools_loaded",
  typeof ToolsLoadedData,
  true
> = event({
  type: "tools_loaded",
  critical: true,
  description:
    "Deferred tools a tool_search call loaded, each by the spec_ref it was pinned with. Appended in the same batch as that call's tool_result, right after it. It renders as a history line with the loaded specs read from their artifacts, so line 0 never changes; the tools stay loaded for the rest of the chain.",
  data: ToolsLoadedData,
});

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
export const PermissionDecision: EventDef<
  "permission_decision",
  typeof PermissionDecisionData,
  true
> = event({
  type: "permission_decision",
  critical: true,
  description: "Precedence is deny > ask > allow.",
  data: PermissionDecisionData,
});

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
  input_event_id: EventId.describe(
    "before_input: the user_input or steer decided on. A deny or failed decision means that input renders nothing. before_model_switch for a revert: the input that caused it.",
  ).optional(),
});
export const HookDecision: EventDef<
  "hook_decision",
  typeof HookDecisionData,
  true
> = event({
  type: "hook_decision",
  critical: true,
  description:
    "A policy hook's decision, durable before any work it gates. Replay reads it and never re-runs the hook. failed means the hook threw or timed out: a gating hook's failure denies; an observation hook's failure changes nothing.",
  data: HookDecisionData,
});

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
export const ApprovalRequested: EventDef<
  "approval_requested",
  typeof ApprovalRequestedData,
  true
> = event({
  type: "approval_requested",
  critical: true,
  description:
    "A single-use challenge bound to one invocation. The full binding (tenant, installation, file hashes) lives in the approvals table; args_hash is recorded because it is what the approver is shown.",
  data: ApprovalRequestedData,
});

export const ApprovalGranted: EventDef<
  "approval_granted",
  typeof ApprovalBinding,
  true,
  typeof ActorWithPrincipal
> = eventWithActor({
  type: "approval_granted",
  critical: true,
  description:
    "actor.principal is the approver, checked against current approver policy at consumption. challenge_id, call_id and args_hash must match the open challenge; the branch is the envelope branch_id.",
  data: ApprovalBinding,
  actor: ActorWithPrincipal,
});

export const ApprovalDenied: EventDef<
  "approval_denied",
  typeof ApprovalBinding,
  true,
  typeof ActorWithPrincipal
> = eventWithActor({
  type: "approval_denied",
  critical: true,
  data: ApprovalBinding,
  actor: ActorWithPrincipal,
});
