import { z } from "zod";
import {
  Actor,
  AdapterRef,
  ArtifactRef,
  ModelRef,
  ModelSettings,
  Principal,
  ToolSpec,
} from "../common";
import { type EventSchema, event } from "../envelope";
import {
  BranchId,
  CallId,
  EventId,
  SandboxId,
  SnapshotId,
  ThreadId,
} from "../ids";
import { Policy } from "../policy";
import {
  Int,
  JsonObject,
  Name,
  NonEmpty,
  PosInt,
  Sha256,
  TimeMs,
} from "../primitives";
import type { Arr, EnumOf, Lit, Opt, Strict } from "../zod-types";

// Thread lifecycle, settings epochs, tool sets, snapshots and branches.

const RELATIONS = ["subagent", "handoff"] as const;
export const ThreadStartedData: Strict<{
  agent_name: typeof NonEmpty;
  config_hash: typeof Sha256;
  model: typeof ModelRef;
  model_params: typeof JsonObject;
  adapter: typeof AdapterRef;
  instructions: z.ZodString;
  tools: Arr<typeof ToolSpec>;
  sandbox_provider: Opt<typeof Name>;
  policy: Opt<typeof Policy>;
  parent: Opt<
    Strict<{
      thread_id: typeof ThreadId;
      branch_id: typeof BranchId;
      event_id: typeof EventId;
      relation: EnumOf<typeof RELATIONS>;
    }>
  >;
}> = z.strictObject({
  agent_name: NonEmpty,
  config_hash: Sha256,
  model: ModelRef,
  model_params: JsonObject,
  adapter: AdapterRef,
  instructions: z.string(),
  tools: z.array(ToolSpec),
  sandbox_provider: Name.optional(),
  policy: Policy.optional(),
  parent: z
    .strictObject({
      thread_id: ThreadId,
      branch_id: BranchId,
      event_id: EventId,
      relation: z.enum(RELATIONS),
    })
    .optional(),
});
export const ThreadStarted: EventSchema<
  "thread_started",
  typeof ThreadStartedData,
  true
> = event("thread_started", true, Actor, ThreadStartedData);

const TOOLS_CAUSES = ["tool_search", "mcp_list_changed", "host"] as const;
export const ToolsChangedData: Strict<{
  tools: Arr<typeof ToolSpec>;
  tools_hash: typeof Sha256;
  cause: Opt<
    Strict<{
      kind: EnumOf<typeof TOOLS_CAUSES>;
      call_id: Opt<typeof CallId>;
      server: Opt<typeof Name>;
    }>
  >;
}> = z.strictObject({
  tools: z.array(ToolSpec),
  tools_hash: Sha256,
  cause: z
    .strictObject({
      kind: z.enum(TOOLS_CAUSES),
      call_id: CallId.optional(),
      server: Name.optional(),
    })
    .optional(),
});
export const ToolsChanged: EventSchema<
  "tools_changed",
  typeof ToolsChangedData,
  true
> = event("tools_changed", true, Actor, ToolsChangedData);

// reason user names an operator principal (actor user, or host acting for it). The automatic
// reasons come from the host or recovery only. Never model or tool.
const OPERATOR_KINDS = ["user", "host"] as const;
const AUTOMATIC_KINDS = ["host", "recovery"] as const;
const SettingsActor: Strict<{
  kind: EnumOf<typeof AUTOMATIC_KINDS>;
  principal: Opt<typeof Principal>;
}> = z.strictObject({
  kind: z.enum(AUTOMATIC_KINDS),
  principal: Principal.optional(),
});
const SettingsOperator: Strict<{
  kind: EnumOf<typeof OPERATOR_KINDS>;
  principal: typeof Principal;
}> = z.strictObject({ kind: z.enum(OPERATOR_KINDS), principal: Principal });
const AUTOMATIC_REASONS = ["fallback", "escalation", "revert"] as const;
type SettingsShape = {
  settings: typeof ModelSettings;
  cause_event_id: Opt<typeof EventId>;
};
const settingsShape: SettingsShape = {
  settings: ModelSettings,
  cause_event_id: EventId.optional(),
};
/** Starts a new settings epoch. */
export const SettingsChanged: z.ZodUnion<
  readonly [
    EventSchema<
      "settings_changed",
      Strict<SettingsShape & { reason: Lit<"user"> }>,
      true,
      typeof SettingsOperator
    >,
    EventSchema<
      "settings_changed",
      Strict<SettingsShape & { reason: EnumOf<typeof AUTOMATIC_REASONS> }>,
      true,
      typeof SettingsActor
    >,
  ]
> = z.union([
  event(
    "settings_changed",
    true,
    SettingsOperator,
    z.strictObject({ ...settingsShape, reason: z.literal("user") }),
  ),
  event(
    "settings_changed",
    true,
    SettingsActor,
    z.strictObject({ ...settingsShape, reason: z.enum(AUTOMATIC_REASONS) }),
  ),
]);

const CAPTURE_CLASSES = [
  "filesystem",
  "filesystem_and_processes",
  "full_vm",
] as const;
export const SnapshotData: Strict<{
  snapshot_id: typeof SnapshotId;
  provider: typeof Name;
  sandbox_id: typeof SandboxId;
  capture_class: EnumOf<typeof CAPTURE_CLASSES>;
  expires_at: z.ZodNullable<typeof TimeMs>;
  manifest_hash: typeof Sha256;
  knowledge_revision: Opt<typeof Int>;
  quiesced: Strict<{
    frozen: Arr<typeof NonEmpty>;
    stopped: Arr<typeof NonEmpty>;
    excluded: Arr<typeof NonEmpty>;
  }>;
}> = z.strictObject({
  snapshot_id: SnapshotId,
  provider: Name,
  sandbox_id: SandboxId,
  capture_class: z.enum(CAPTURE_CLASSES),
  expires_at: TimeMs.nullable(),
  manifest_hash: Sha256,
  knowledge_revision: Int.optional(),
  quiesced: z.strictObject({
    frozen: z.array(NonEmpty),
    stopped: z.array(NonEmpty),
    excluded: z.array(NonEmpty),
  }),
});
export const Snapshot: EventSchema<"snapshot", typeof SnapshotData, true> =
  event("snapshot", true, Actor, SnapshotData);

type ForkShape = { parent_branch_id: typeof BranchId; at_hash: typeof Sha256 };
const forkShape: ForkShape = { parent_branch_id: BranchId, at_hash: Sha256 };
const KNOWLEDGE_POLICIES = ["pinned", "current"] as const;
// sandbox_id and knowledge_policy are present exactly when the fork restores a snapshot.
export const ForkData: z.ZodDiscriminatedUnion<
  [
    Strict<
      ForkShape & {
        reason: Lit<"snapshot">;
        sandbox_id: typeof SandboxId;
        knowledge_policy: EnumOf<typeof KNOWLEDGE_POLICIES>;
      }
    >,
    Strict<ForkShape & { reason: Lit<"repair"> }>,
  ],
  "reason"
> = z.discriminatedUnion("reason", [
  z.strictObject({
    ...forkShape,
    reason: z.literal("snapshot"),
    sandbox_id: SandboxId,
    knowledge_policy: z.enum(KNOWLEDGE_POLICIES),
  }),
  z.strictObject({ ...forkShape, reason: z.literal("repair") }),
]);
export const Fork: EventSchema<"fork", typeof ForkData, true> = event(
  "fork",
  true,
  Actor,
  ForkData,
);

export const LogRepairedData: Strict<{
  truncated_bytes: typeof PosInt;
  at_offset: typeof Int;
  dropped_ref: Opt<typeof ArtifactRef>;
}> = z.strictObject({
  truncated_bytes: PosInt,
  at_offset: Int,
  dropped_ref: ArtifactRef.optional(),
});
export const LogRepaired: EventSchema<
  "log_repaired",
  typeof LogRepairedData,
  false
> = event("log_repaired", false, Actor, LogRepairedData);
