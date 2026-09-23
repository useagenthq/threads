import { Input } from "@threads/core";
import {
  BranchId,
  Budget,
  EventId,
  JsonObject,
  ModelRef,
  NonEmpty,
  PermissionMode,
  PermissionRule,
  ThreadId,
} from "@threads/core/host";
import { z } from "zod";

// Request bodies of the host HTTP API (spec/schema/host-api/host-api.v1.schema.json), built
// from core's wire schemas. Every body is parsed here before a handler sees it (trust boundary).

export const StartRunRequest: z.ZodObject<{
  agent: typeof NonEmpty;
  input: typeof Input;
  thread_id: z.ZodOptional<typeof ThreadId>;
  branch_id: z.ZodOptional<typeof BranchId>;
  budget: z.ZodOptional<typeof Budget>;
}> = z.strictObject({
  agent: NonEmpty,
  input: Input,
  thread_id: ThreadId.optional(),
  branch_id: BranchId.optional(),
  budget: Budget.optional(),
});
export type StartRunRequest = z.infer<typeof StartRunRequest>;

export const ApprovalDecision: z.ZodObject<{
  decision: z.ZodEnum<{ grant: "grant"; deny: "deny" }>;
  remember_rule: z.ZodOptional<typeof PermissionRule>;
  reason: z.ZodOptional<z.ZodString>;
}> = z.strictObject({
  decision: z.enum(["grant", "deny"]),
  remember_rule: PermissionRule.optional(),
  reason: z.string().optional(),
});

export const Answer: z.ZodObject<{
  answer: z.ZodUnion<readonly [z.ZodString, z.ZodArray<z.ZodString>]>;
}> = z.strictObject({
  answer: z.union([z.string().min(1), z.array(z.string()).min(1)]),
});

export const ParkedResolution: z.ZodObject<{
  resolution: z.ZodEnum<{
    assume_done: "assume_done";
    assume_not_done: "assume_not_done";
  }>;
}> = z.strictObject({ resolution: z.enum(["assume_done", "assume_not_done"]) });

export const SettingsChange: z.ZodObject<{
  model: typeof ModelRef;
  model_params: z.ZodOptional<typeof JsonObject>;
  reasoning_carryover: z.ZodOptional<
    z.ZodEnum<{ keep: "keep"; omit_prior: "omit_prior" }>
  >;
}> = z.strictObject({
  model: ModelRef,
  model_params: JsonObject.optional(),
  reasoning_carryover: z.enum(["keep", "omit_prior"]).optional(),
});

export const ModeChange: z.ZodObject<{ mode: typeof PermissionMode }> =
  z.strictObject({ mode: PermissionMode });

export const ForkRequest: z.ZodObject<{
  event_id: typeof EventId;
  mode: z.ZodOptional<z.ZodEnum<{ live: "live"; stub: "stub" }>>;
  knowledge: z.ZodOptional<z.ZodEnum<{ pinned: "pinned"; current: "current" }>>;
}> = z.strictObject({
  event_id: EventId,
  mode: z.enum(["live", "stub"]).optional(),
  knowledge: z.enum(["pinned", "current"]).optional(),
});

/** The Idempotency-Key header (openapi.json): 1 to 255 characters. */
export const IdempotencyKey: z.ZodString = z.string().min(1).max(255);
