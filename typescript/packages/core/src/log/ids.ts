import { z } from "zod";
import { UUID_PATTERN } from "./primitives";
import type { Brand } from "./zod-types";

// Each id is its own schema instance so the JSON Schema export names it in $defs.

export const ThreadId: Brand<z.ZodString, "ThreadId"> = z
  .string()
  .regex(UUID_PATTERN)
  .meta({ id: "ThreadId" })
  .brand<"ThreadId">();
export type ThreadId = z.infer<typeof ThreadId>;

export const BranchId: Brand<z.ZodString, "BranchId"> = z
  .string()
  .regex(UUID_PATTERN)
  .meta({ id: "BranchId" })
  .brand<"BranchId">();
export type BranchId = z.infer<typeof BranchId>;

export const EventId: Brand<z.ZodString, "EventId"> = z
  .string()
  .regex(UUID_PATTERN)
  .meta({ id: "EventId" })
  .brand<"EventId">();
export type EventId = z.infer<typeof EventId>;

export const ChallengeId: Brand<z.ZodString, "ChallengeId"> = z
  .string()
  .regex(UUID_PATTERN)
  .meta({ id: "ChallengeId" })
  .brand<"ChallengeId">();
export type ChallengeId = z.infer<typeof ChallengeId>;

/** Opaque, may be provider-issued. The effect key is derived: `<branch_id of the tool_call>:<call_id>`. */
export const CallId: Brand<z.ZodString, "CallId"> = z
  .string()
  .regex(/^[A-Za-z0-9_.-]{1,128}$/)
  .meta({ id: "CallId" })
  .brand<"CallId">();
export type CallId = z.infer<typeof CallId>;

export const SandboxId: Brand<z.ZodString, "SandboxId"> = z
  .string()
  .min(1)
  .meta({ id: "SandboxId" })
  .brand<"SandboxId">();
export type SandboxId = z.infer<typeof SandboxId>;

export const SnapshotId: Brand<z.ZodString, "SnapshotId"> = z
  .string()
  .min(1)
  .meta({ id: "SnapshotId" })
  .brand<"SnapshotId">();
export type SnapshotId = z.infer<typeof SnapshotId>;

export const OccurrenceId: Brand<z.ZodString, "OccurrenceId"> = z
  .string()
  .min(1)
  .meta({ id: "OccurrenceId" })
  .brand<"OccurrenceId">();
export type OccurrenceId = z.infer<typeof OccurrenceId>;
