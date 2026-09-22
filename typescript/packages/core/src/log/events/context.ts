import { z } from "zod";
import {
  ActorWithPrincipal,
  ArtifactRef,
  PermissionMode,
  PermissionRule,
  Span,
} from "../common";
import { type EventDef, event, eventWithActor } from "../envelope";
import { CallId, ChallengeId, EventId } from "../ids";
import { Int, JsonValue, PosInt, Sha256 } from "../primitives";
import { type Ruled, withRule } from "../rules";
import type { Arr, EnumOf, Opt, Strict } from "../zod-types";

// Context management, structured output and permission modes.

const COMPACT_TRIGGERS = ["threshold", "reactive", "manual"] as const;
const OUTPUT_OUTCOMES = ["accepted", "rejected"] as const;
const EDIT_REASONS = [
  "threshold",
  "idle",
  "provider",
  "guardrail",
  "manual",
  "compaction_fallback",
] as const;
const EDIT_ACTIONS = ["clear", "redact"] as const;
const COMPACTION_STAGES = ["hook", "summary", "restore"] as const;
const COMPACTION_FAILURES = [
  "hook_denied",
  "model_error",
  "prompt_too_long",
  "empty_summary",
  "artifact_error",
  "still_over_threshold",
] as const;
const RULE_DECISIONS = ["allow", "deny"] as const;
const PREFLIGHT_ACTIONS = ["compact", "fail"] as const;

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
export const Compacted: EventDef<"compacted", typeof CompactedData, true> =
  event({
    type: "compacted",
    critical: true,
    description:
      "Render replaces events from..to with the summary as untrusted reference. Both ends are step boundaries (no pending call, no open model attempt), so a tool pair is never split (semantic rule 10). The log is untouched, and the declared prefix (line 0) is never compacted. When summary_request_event_id is set, summary_ref is the UTF-8 text of that compaction request's response.",
    data: CompactedData,
  });

const OUTPUT_VALIDATED_DATA_RULE = {
  if: { properties: { outcome: { const: "accepted" } } },
  then: { required: ["value"], not: { required: ["errors"] } },
  else: { required: ["errors"], not: { required: ["value"] } },
} as const;
export const OutputValidatedData: Ruled<
  Strict<{
    source_event_id: typeof EventId;
    schema_sha256: typeof Sha256;
    outcome: EnumOf<typeof OUTPUT_OUTCOMES>;
    value: Opt<typeof JsonValue>;
    errors: Opt<Arr<Strict<{ path: z.ZodString; message: z.ZodString }>>>;
  }>,
  typeof OUTPUT_VALIDATED_DATA_RULE
> = withRule(
  z.strictObject({
    source_event_id: EventId,
    schema_sha256: Sha256,
    outcome: z.enum(OUTPUT_OUTCOMES),
    value: JsonValue.optional(),
    errors: z
      .array(z.strictObject({ path: z.string(), message: z.string() }))
      .optional(),
  }),
  OUTPUT_VALIDATED_DATA_RULE,
);
export const OutputValidated: EventDef<
  "output_validated",
  typeof OutputValidatedData,
  true
> = event({
  type: "output_validated",
  critical: true,
  description:
    "Structured-output check of one candidate. source_event_id is the raw candidate: the final_output tool_call, or the model_response (native mode). accepted carries the validated value; rejected carries the errors shown to the model. schema_sha256 must equal policy.output.schema_sha256, and an accepted value must validate against that schema.",
  data: OutputValidatedData,
});

const REDACTION_RULE = {
  if: { properties: { action: { const: "redact" } } },
  then: { required: ["part", "spans"] },
  else: {
    not: { anyOf: [{ required: ["part"] }, { required: ["spans"] }] },
  },
} as const;
// part and spans are present exactly on a redaction.
export const ContextEdit: Ruled<
  Strict<{
    call_id: typeof CallId;
    action: EnumOf<typeof EDIT_ACTIONS>;
    part: Opt<typeof Int>;
    spans: Opt<Arr<typeof Span>>;
  }>,
  typeof REDACTION_RULE
> = withRule(
  z.strictObject({
    call_id: CallId,
    action: z.enum(EDIT_ACTIONS),
    part: Int.optional(),
    spans: z.array(Span).min(1).optional(),
  }),
  REDACTION_RULE,
);

const CONTEXT_EDITED_DATA_RULE = {
  if: { properties: { reason: { const: "provider" } } },
  then: { required: ["request_event_id"] },
  else: { not: { required: ["request_event_id"] } },
} as const;
export const ContextEditedData: Ruled<
  Strict<{
    reason: EnumOf<typeof EDIT_REASONS>;
    request_event_id: Opt<typeof EventId>;
    edits: Arr<typeof ContextEdit>;
  }>,
  typeof CONTEXT_EDITED_DATA_RULE
> = withRule(
  z.strictObject({
    reason: z.enum(EDIT_REASONS),
    request_event_id: EventId.describe(
      "reason provider only: the attempt whose effective context the provider edited, as the adapter reported it. That attempt's effective context is non-exact for replay.",
    ).optional(),
    edits: z.array(ContextEdit).min(1),
  }),
  CONTEXT_EDITED_DATA_RULE,
);
export const ContextEdited: EventDef<
  "context_edited",
  typeof ContextEditedData,
  true
> = event({
  type: "context_edited",
  critical: true,
  description:
    "Changes how earlier tool results render, never the results themselves. clear replaces the call's result lines with the fixed placeholder; redact replaces spans of one text part (part 0 is the preview when there is no content) with [redacted]. Every call_id must have a recorded result.",
  data: ContextEditedData,
});

export const CompactionFailedData: Strict<{
  stage: EnumOf<typeof COMPACTION_STAGES>;
  reason: EnumOf<typeof COMPACTION_FAILURES>;
  request_event_id: Opt<typeof EventId>;
}> = z.strictObject({
  stage: z.enum(COMPACTION_STAGES),
  reason: z.enum(COMPACTION_FAILURES),
  request_event_id: EventId.optional(),
});
export const CompactionFailed: EventDef<
  "compaction_failed",
  typeof CompactionFailedData,
  true
> = event({
  type: "compaction_failed",
  critical: true,
  description:
    "A compaction attempt failed. Consecutive failures since the last compacted open the circuit breaker (derived, ).",
  data: CompactionFailedData,
});

export const ModeChangedData: Strict<{
  from: typeof PermissionMode;
  to: typeof PermissionMode;
  cause_event_id: Opt<typeof EventId>;
}> = z.strictObject({
  from: PermissionMode,
  to: PermissionMode,
  cause_event_id: EventId.optional(),
});
export const ModeChanged: EventDef<
  "mode_changed",
  typeof ModeChangedData,
  true
> = event({
  type: "mode_changed",
  critical: true,
  description:
    "Permission mode change. from must be the current mode; to bypass needs policy.permissions.allow_bypass. Never caused by the agent alone: an operator via the host API, or an approved exit_plan_mode call (cause_event_id).",
  data: ModeChangedData,
});

export const PermissionRuleAddedData: Strict<{
  rule: typeof PermissionRule;
  decision: EnumOf<typeof RULE_DECISIONS>;
  challenge_id: typeof ChallengeId;
}> = z.strictObject({
  rule: PermissionRule,
  decision: z.enum(RULE_DECISIONS),
  challenge_id: ChallengeId,
});
export const PermissionRuleAdded: EventDef<
  "permission_rule_added",
  typeof PermissionRuleAddedData,
  true,
  typeof ActorWithPrincipal
> = eventWithActor({
  type: "permission_rule_added",
  critical: true,
  description:
    "A thread-scoped rule an approver chose while answering a challenge ('allow for this thread'). Checked after self-config, deny rules and protected paths, so it can never open those.",
  data: PermissionRuleAddedData,
  actor: ActorWithPrincipal,
});

export const ContextPreflightBlockedData: Strict<{
  estimated_tokens: typeof Int;
  window_tokens: typeof Int;
  action: EnumOf<typeof PREFLIGHT_ACTIONS>;
}> = z.strictObject({
  estimated_tokens: Int,
  window_tokens: Int,
  action: z.enum(PREFLIGHT_ACTIONS),
});
export const ContextPreflightBlocked: EventDef<
  "context_preflight_blocked",
  typeof ContextPreflightBlockedData,
  true
> = event({
  type: "context_preflight_blocked",
  critical: true,
  description:
    "before any model_request exists, the context estimate reached the effective window, so no request was created, sent or billed. action compact runs the once-per-step reactive compaction; fail ends the turn context_exhausted. estimated_tokens is an estimate (rendered bytes / 4), labeled as such.",
  data: ContextPreflightBlockedData,
});
