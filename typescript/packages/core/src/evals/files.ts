import { z } from "zod";
import {
  ArtifactRef,
  CallId,
  EventId,
  HookDecisionData,
  InjectedData,
  Name,
  PosInt,
  ResultPart,
  Sha256,
  Span,
} from "../log";
import type { Arr, EnumOf, Lit, Opt, Strict } from "../log/zod-types";

// What saveCase writes beside the log and the runner reads back (spec/schema/eval.v1.schema.json,
// referenced from spec/conformance/case.schema.json): the recorded read-only results, the
// recorded hook and recall outcomes, and the case.json fields this lane adds.

export const SandboxResult: Strict<{
  tool: z.ZodString;
  args_hash: typeof Sha256;
  occurrence: typeof PosInt;
  is_error: z.ZodBoolean;
  preview: z.ZodString;
  content: Opt<Arr<typeof ResultPart>>;
  ref: Opt<typeof ArtifactRef>;
}> = z
  .strictObject({
    tool: z.string(),
    args_hash: Sha256.describe("sha256 of the RFC 8785 input."),
    occurrence: PosInt.describe("Counts from 1 per (tool, args_hash)."),
    is_error: z.boolean(),
    preview: z.string(),
    content: z.array(ResultPart).min(1).optional(),
    ref: ArtifactRef.describe(
      "The spilled full output; its bytes are in the case's artifacts/.",
    ).optional(),
  })
  .meta({ id: "SandboxResult" });
export type SandboxResult = z.infer<typeof SandboxResult>;

/** sandbox.json v2: one recorded result per read-only call, keyed like stubs.json. */
export const SandboxResults: Strict<{
  version: Lit<2>;
  results: Arr<typeof SandboxResult>;
}> = z
  .strictObject({ version: z.literal(2), results: z.array(SandboxResult) })
  .meta({ id: "SandboxResults" });
export type SandboxResults = z.infer<typeof SandboxResults>;

const ByCall: Strict<{ call_id: typeof CallId }> = z.strictObject({
  call_id: CallId,
});
const ByOccurrence: Strict<{ occurrence: typeof PosInt }> = z.strictObject({
  occurrence: PosInt,
});

export const HookRecord: Strict<{
  extension: typeof Name;
  hook: (typeof HookDecisionData)["shape"]["hook"];
  occurrence: typeof PosInt;
  at: Opt<z.ZodUnion<readonly [typeof ByCall, typeof ByOccurrence]>>;
  decision: (typeof HookDecisionData)["shape"]["decision"];
  reason: Opt<z.ZodString>;
  injected: Arr<typeof InjectedData>;
  spans: Opt<Arr<typeof Span>>;
}> = z
  .strictObject({
    extension: Name,
    hook: HookDecisionData.shape.hook,
    occurrence: PosInt.describe("Counts from 1 per (extension, hook)."),
    at: z
      .union([ByCall, ByOccurrence])
      .describe(
        "Observation records only: the call this record belongs to (call_id), or the occurrence of its trigger within the turn.",
      )
      .optional(),
    decision: HookDecisionData.shape.decision,
    reason: z.string().optional(),
    injected: z
      .array(InjectedData)
      .describe(
        "The injected{source: hook} events the decision produced, in order.",
      ),
    spans: z
      .array(Span)
      .describe("before_tool_result redact: the spans the decision redacted.")
      .optional(),
  })
  .meta({ id: "HookRecord" });
export type HookRecord = z.infer<typeof HookRecord>;

const RECALL_SOURCES = ["memory", "knowledge"] as const;
export const RecallRecord: Strict<{
  source: EnumOf<typeof RECALL_SOURCES>;
  occurrence: typeof PosInt;
  items: Arr<typeof InjectedData>;
}> = z
  .strictObject({
    source: z.enum(RECALL_SOURCES),
    occurrence: PosInt.describe("Counts from 1 per source."),
    items: z.array(InjectedData),
  })
  .meta({ id: "RecallRecord" });
export type RecallRecord = z.infer<typeof RecallRecord>;

/** extensions.json: the turn's hook decisions and recall, replayed by stand-ins offline. */
export const ExtensionScript: Strict<{
  hooks: Arr<typeof HookRecord>;
  recall: Arr<typeof RecallRecord>;
}> = z
  .strictObject({
    hooks: z.array(HookRecord),
    recall: z.array(RecallRecord),
  })
  .meta({ id: "ExtensionScript" });
export type ExtensionScript = z.infer<typeof ExtensionScript>;

const OFFLINE_REASONS = [
  "artifact_missing",
  "unsettled_effect",
  "content_input",
  "child_threads",
  "team_calls",
  "extension_events",
] as const;
/** Why saveCase marked a case not runnable offline. */
export const OfflineReason: EnumOf<typeof OFFLINE_REASONS> = z
  .enum(OFFLINE_REASONS)
  .meta({ id: "OfflineReason" });
export type OfflineReason = z.infer<typeof OfflineReason>;

export const CaseOffline: Strict<{
  runnable: Lit<false>;
  reason: typeof OfflineReason;
  types: Opt<Arr<z.ZodString>>;
}> = z
  .strictObject({
    runnable: z.literal(false),
    reason: OfflineReason,
    types: z
      .array(z.string())
      .describe("extension_events: the event types the rerun can't script.")
      .optional(),
  })
  .meta({ id: "CaseOffline" });

export const CaseSnapshot: Strict<{
  event_id: typeof EventId;
  provider: typeof Name;
}> = z.strictObject({ event_id: EventId, provider: Name }).meta({
  id: "CaseSnapshot",
  description:
    "A sandbox snapshot at the case's restore point; only a live run restores it.",
});

export const CaseLine0: Strict<{ sha256: typeof Sha256 }> = z
  .strictObject({ sha256: Sha256 })
  .meta({
    id: "CaseLine0",
    description: "sha256 of line0.json: the recorded Render v1 line 0 bytes.",
  });
