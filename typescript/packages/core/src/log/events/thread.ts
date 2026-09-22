import { z } from "zod";
import {
  Actor,
  ActorWithPrincipal,
  AdapterRef,
  ArtifactRef,
  ModelRef,
  ModelSettings,
  ToolSpec,
} from "../common";
import { type EventDef, event, eventWithActor } from "../envelope";
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
import { type Ruled, withRule } from "../rules";
import type { Arr, EnumOf, Opt, Strict } from "../zod-types";

// Thread lifecycle, settings epochs, tool sets, snapshots and branches.

const RELATIONS = ["subagent", "handoff"] as const;
const TOOLS_CAUSES = ["tool_search", "mcp_list_changed", "host"] as const;
const SETTINGS_REASONS = ["user", "fallback", "escalation", "revert"] as const;
const CAPTURE_CLASSES = [
  "filesystem",
  "filesystem_and_processes",
  "full_vm",
] as const;
const FORK_REASONS = ["snapshot", "repair"] as const;
const KNOWLEDGE_POLICIES = ["pinned", "current"] as const;

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
  config_hash: Sha256.describe(
    "sha256 of the RFC 8785 bytes of the resolved, secret-free agent config.",
  ),
  model: ModelRef,
  model_params: JsonObject.describe(
    "Every adapter-visible generation parameter (max_tokens, temperature, tool_choice, ...).",
  ),
  adapter: AdapterRef,
  instructions: z
    .string()
    .describe(
      "The full pinned system text: base instructions, extension instructions in order, skill listing, memory guidance.",
    ),
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
    .describe(
      "Set on a child thread: the parent event that created it (agent_spawned or handoff).",
    )
    .optional(),
});
export const ThreadStarted: EventDef<
  "thread_started",
  typeof ThreadStartedData,
  true
> = event({
  type: "thread_started",
  critical: true,
  description:
    "Pins everything in Render v1 line 0 (the declared immutable prefix) for the thread.",
  data: ThreadStartedData,
});

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
    .describe(
      "Provenance. tool_search: the call whose result loaded deferred tools (those specs lose defer_loading). mcp_list_changed: a server's list changed. Absent means host.",
    )
    .optional(),
});
export const ToolsChanged: EventDef<
  "tools_changed",
  typeof ToolsChangedData,
  true
> = event({
  type: "tools_changed",
  critical: true,
  description:
    "The model-visible tool set changed mid-thread (for example an MCP server's tool list). tools is the complete new set; tools_hash is sha256 of its RFC 8785 bytes. It renders as a line after the declared prefix, so line 0 stays equal (C7) while the log still records exactly what the model saw (C6). Later calls derive effect_class and dedup_window_ms from the latest set.",
  data: ToolsChangedData,
});

export const SettingsChangedData: Strict<{
  reason: EnumOf<typeof SETTINGS_REASONS>;
  settings: typeof ModelSettings;
  cause_event_id: Opt<typeof EventId>;
}> = z.strictObject({
  reason: z.enum(SETTINGS_REASONS),
  settings: ModelSettings,
  cause_event_id: EventId.optional(),
});
export const SettingsChanged: EventDef<
  "settings_changed",
  typeof SettingsChangedData,
  true,
  typeof Actor
> = eventWithActor({
  type: "settings_changed",
  critical: true,
  description:
    "Starts a new settings epoch: Render v1 line 0 takes model, params and adapter from here, system and tools stay pinned. C7 holds within each epoch. Never while a model attempt awaits its response. model must be listed in policy.models when policy is present.",
  data: SettingsChangedData,
  actor: withRule(
    Actor,
    {
      allOf: [{ properties: { kind: { enum: ["host", "user", "recovery"] } } }],
    },
    {
      description:
        "reason user: an operator principal, as actor user or host (the host API acting for that principal); the host still authorizes the principal, the schema only proves one is named. reason fallback, escalation or revert: automatic, so actor host or recovery only. Never model or tool.",
    },
  ),
  rule: {
    allOf: [
      {
        if: {
          properties: { data: { properties: { reason: { const: "user" } } } },
        },
        then: {
          properties: {
            actor: {
              allOf: [
                { $ref: ActorWithPrincipal },
                { properties: { kind: { enum: ["user", "host"] } } },
              ],
            },
          },
        },
        else: {
          properties: {
            actor: { properties: { kind: { enum: ["host", "recovery"] } } },
          },
        },
      },
    ],
  },
});

export const SnapshotData: Strict<{
  snapshot_id: typeof SnapshotId;
  provider: typeof Name;
  sandbox_id: typeof SandboxId;
  capture_class: EnumOf<typeof CAPTURE_CLASSES>;
  expires_at: z.ZodXor<readonly [typeof TimeMs, z.ZodNull]>;
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
  expires_at: z.xor([TimeMs, z.null()]),
  manifest_hash: Sha256.describe(
    "RFC 8785 hash of the captured file-tree manifest (path, mode, size, sha256).",
  ),
  knowledge_revision: Int.describe(
    "The host knowledge store revision at capture. Present when the thread has a knowledge binding. A fork with knowledge_policy pinned searches as of this revision.",
  ).optional(),
  quiesced: z
    .strictObject({
      frozen: z.array(NonEmpty),
      stopped: z.array(NonEmpty),
      excluded: z.array(NonEmpty),
    })
    .describe(
      "How sandbox processes that could mutate captured state were handled. excluded lists only processes the provider proves cannot write captured paths.",
    ),
});
export const Snapshot: EventDef<"snapshot", typeof SnapshotData, true> = event({
  type: "snapshot",
  critical: true,
  description:
    "A completed sandbox capture of the state after every event before this one. Appended only after the provider reports it durable and restorable. The event itself is the fork point.",
  data: SnapshotData,
});

const FORK_DATA_RULE = {
  if: { properties: { reason: { const: "snapshot" } } },
  then: { required: ["sandbox_id", "knowledge_policy"] },
  else: {
    allOf: [
      { not: { required: ["sandbox_id"] } },
      { not: { required: ["knowledge_policy"] } },
    ],
  },
} as const;
export const ForkData: Ruled<
  Strict<{
    parent_branch_id: typeof BranchId;
    at_hash: typeof Sha256;
    reason: EnumOf<typeof FORK_REASONS>;
    sandbox_id: Opt<typeof SandboxId>;
    knowledge_policy: Opt<EnumOf<typeof KNOWLEDGE_POLICIES>>;
  }>,
  typeof FORK_DATA_RULE
> = withRule(
  z.strictObject({
    parent_branch_id: BranchId,
    at_hash: Sha256.describe(
      "sha256 of the parent's line at at_seq. Binds the child to the parent's chain.",
    ),
    reason: z
      .enum(FORK_REASONS)
      .describe(
        "snapshot: a user fork at an eligible snapshot event. repair: operator repair of a corrupt parent at its last valid line; no sandbox is restored and the child is inspection-only (never runnable; ).",
      ),
    sandbox_id: SandboxId.describe(
      "The isolated child sandbox restored from the snapshot, with its manifest verified.",
    ).optional(),
    knowledge_policy: z
      .enum(KNOWLEDGE_POLICIES)
      .describe(
        "How the child searches knowledge. pinned (the API default): as of the fork snapshot's knowledge_revision. current: the live corpus. Recorded retrievals always replay as recorded.",
      )
      .optional(),
  }),
  FORK_DATA_RULE,
);
export const Fork: EventDef<"fork", typeof ForkData, true> = event({
  type: "fork",
  critical: true,
  description:
    "First event of a child branch, right after the child's own header (prev_hash = sha256 of that header). The parent's rows are referenced, never copied. Derived, not stored: at_seq = this event's seq - 1; for reason snapshot the fork point is the parent's snapshot event at at_seq; the thread is the envelope thread_id.",
  data: ForkData,
});

export const LogRepairedData: Strict<{
  truncated_bytes: typeof PosInt;
  at_offset: typeof Int;
  dropped_ref: Opt<typeof ArtifactRef>;
}> = z.strictObject({
  truncated_bytes: PosInt,
  at_offset: Int,
  dropped_ref: ArtifactRef.optional(),
});
export const LogRepaired: EventDef<
  "log_repaired",
  typeof LogRepairedData,
  false
> = event({
  type: "log_repaired",
  critical: false,
  description:
    "Evidence of an import that dropped a torn tail (JSONL framing only; the SQLite store cannot tear). The dropped bytes are kept as an artifact.",
  data: LogRepairedData,
});
