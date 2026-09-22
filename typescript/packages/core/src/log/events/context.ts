import { z } from "zod";
import {
  Actor,
  ActorWithPrincipal,
  ArtifactRef,
  PermissionMode,
  PermissionRule,
  Span,
} from "../common";
import { type EventSchema, event } from "../envelope";
import { CallId, ChallengeId, EventId } from "../ids";
import { Int, JsonValue, PosInt, Sha256 } from "../primitives";
import type { Arr, EnumOf, Lit, Opt, Strict } from "../zod-types";

// Context management, structured output and permission modes.

const COMPACT_TRIGGERS = ["threshold", "reactive", "manual"] as const;
/** Render replaces events from..to with the summary as untrusted reference. The log is untouched. */
export const CompactedData: Strict<{
  from_seq: typeof PosInt;
  to_seq: typeof PosInt;
  from_event_id: typeof EventId;
  to_event_id: typeof EventId;
  summary_ref: typeof ArtifactRef;
  summary_request_event_id: Opt<typeof EventId>;
  trigger: Opt<EnumOf<typeof COMPACT_TRIGGERS>>;
}> = z.strictObject({
  from_seq: PosInt,
  to_seq: PosInt,
  from_event_id: EventId,
  to_event_id: EventId,
  summary_ref: ArtifactRef,
  summary_request_event_id: EventId.optional(),
  trigger: z.enum(COMPACT_TRIGGERS).optional(),
});
export const Compacted: EventSchema<"compacted", typeof CompactedData, true> =
  event("compacted", true, Actor, CompactedData);

// part and spans are present exactly on a redaction.
export const ContextEdit: z.ZodDiscriminatedUnion<
  [
    Strict<{ call_id: typeof CallId; action: Lit<"clear"> }>,
    Strict<{
      call_id: typeof CallId;
      action: Lit<"redact">;
      part: typeof Int;
      spans: Arr<typeof Span>;
    }>,
  ],
  "action"
> = z.discriminatedUnion("action", [
  z.strictObject({ call_id: CallId, action: z.literal("clear") }),
  z.strictObject({
    call_id: CallId,
    action: z.literal("redact"),
    part: Int,
    spans: z.array(Span).min(1),
  }),
]);
const LOCAL_EDIT_REASONS = [
  "threshold",
  "idle",
  "guardrail",
  "manual",
  "compaction_fallback",
] as const;
// request_event_id is present exactly when the provider edited the context.
export const ContextEditedData: z.ZodDiscriminatedUnion<
  [
    Strict<{
      reason: Lit<"provider">;
      request_event_id: typeof EventId;
      edits: Arr<typeof ContextEdit>;
    }>,
    Strict<{
      reason: EnumOf<typeof LOCAL_EDIT_REASONS>;
      edits: Arr<typeof ContextEdit>;
    }>,
  ],
  "reason"
> = z.discriminatedUnion("reason", [
  z.strictObject({
    reason: z.literal("provider"),
    request_event_id: EventId,
    edits: z.array(ContextEdit).min(1),
  }),
  z.strictObject({
    reason: z.enum(LOCAL_EDIT_REASONS),
    edits: z.array(ContextEdit).min(1),
  }),
]);
export const ContextEdited: EventSchema<
  "context_edited",
  typeof ContextEditedData,
  true
> = event("context_edited", true, Actor, ContextEditedData);

const COMPACTION_STAGES = ["hook", "summary", "restore"] as const;
const COMPACTION_FAILURES = [
  "hook_denied",
  "model_error",
  "prompt_too_long",
  "empty_summary",
  "artifact_error",
  "still_over_threshold",
] as const;
export const CompactionFailedData: Strict<{
  stage: EnumOf<typeof COMPACTION_STAGES>;
  reason: EnumOf<typeof COMPACTION_FAILURES>;
  request_event_id: Opt<typeof EventId>;
}> = z.strictObject({
  stage: z.enum(COMPACTION_STAGES),
  reason: z.enum(COMPACTION_FAILURES),
  request_event_id: EventId.optional(),
});
export const CompactionFailed: EventSchema<
  "compaction_failed",
  typeof CompactionFailedData,
  true
> = event("compaction_failed", true, Actor, CompactionFailedData);

const PREFLIGHT_ACTIONS = ["compact", "fail"] as const;
/** The context estimate reached the window before any model_request existed. */
export const ContextPreflightBlockedData: Strict<{
  estimated_tokens: typeof Int;
  window_tokens: typeof Int;
  action: EnumOf<typeof PREFLIGHT_ACTIONS>;
}> = z.strictObject({
  estimated_tokens: Int,
  window_tokens: Int,
  action: z.enum(PREFLIGHT_ACTIONS),
});
export const ContextPreflightBlocked: EventSchema<
  "context_preflight_blocked",
  typeof ContextPreflightBlockedData,
  true
> = event(
  "context_preflight_blocked",
  true,
  Actor,
  ContextPreflightBlockedData,
);

export const OutputError: Strict<{ path: z.ZodString; message: z.ZodString }> =
  z.strictObject({
    path: z.string(),
    message: z.string(),
  });
// accepted carries the value, rejected carries the errors shown to the model.
export const OutputValidatedData: z.ZodDiscriminatedUnion<
  [
    Strict<{
      source_event_id: typeof EventId;
      schema_sha256: typeof Sha256;
      outcome: Lit<"accepted">;
      value: typeof JsonValue;
    }>,
    Strict<{
      source_event_id: typeof EventId;
      schema_sha256: typeof Sha256;
      outcome: Lit<"rejected">;
      errors: Arr<typeof OutputError>;
    }>,
  ],
  "outcome"
> = z.discriminatedUnion("outcome", [
  z.strictObject({
    source_event_id: EventId,
    schema_sha256: Sha256,
    outcome: z.literal("accepted"),
    value: JsonValue,
  }),
  z.strictObject({
    source_event_id: EventId,
    schema_sha256: Sha256,
    outcome: z.literal("rejected"),
    errors: z.array(OutputError),
  }),
]);
export const OutputValidated: EventSchema<
  "output_validated",
  typeof OutputValidatedData,
  true
> = event("output_validated", true, Actor, OutputValidatedData);

export const ModeChangedData: Strict<{
  from: typeof PermissionMode;
  to: typeof PermissionMode;
  cause_event_id: Opt<typeof EventId>;
}> = z.strictObject({
  from: PermissionMode,
  to: PermissionMode,
  cause_event_id: EventId.optional(),
});
export const ModeChanged: EventSchema<
  "mode_changed",
  typeof ModeChangedData,
  true
> = event("mode_changed", true, Actor, ModeChangedData);

const RULE_DECISIONS = ["allow", "deny"] as const;
/** A thread-scoped rule an approver chose while answering a challenge. */
export const PermissionRuleAddedData: Strict<{
  rule: typeof PermissionRule;
  decision: EnumOf<typeof RULE_DECISIONS>;
  challenge_id: typeof ChallengeId;
}> = z.strictObject({
  rule: PermissionRule,
  decision: z.enum(RULE_DECISIONS),
  challenge_id: ChallengeId,
});
export const PermissionRuleAdded: EventSchema<
  "permission_rule_added",
  typeof PermissionRuleAddedData,
  true,
  typeof ActorWithPrincipal
> = event(
  "permission_rule_added",
  true,
  ActorWithPrincipal,
  PermissionRuleAddedData,
);
