import { z } from "zod";
import { NonEmpty, Uuid } from "./primitives";
import type { Brand } from "./zod-types";

// An id with its own `.meta` id is its own def in the export; the others are brands of their
// wire shape, which the schema names directly (Uuid, NonEmpty).

export const ThreadId: Brand<z.ZodString, "ThreadId"> = Uuid.meta({
  id: "ThreadId",
}).brand<"ThreadId">();
export type ThreadId = z.infer<typeof ThreadId>;

export const BranchId: Brand<z.ZodString, "BranchId"> = Uuid.meta({
  id: "BranchId",
}).brand<"BranchId">();
export type BranchId = z.infer<typeof BranchId>;

export const EventId: Brand<z.ZodString, "EventId"> = Uuid.meta({
  id: "EventId",
}).brand<"EventId">();
export type EventId = z.infer<typeof EventId>;

export const ChallengeId: Brand<z.ZodString, "ChallengeId"> =
  Uuid.brand<"ChallengeId">();
export type ChallengeId = z.infer<typeof ChallengeId>;

export const CallId: Brand<z.ZodString, "CallId"> = z
  .string()
  .regex(/^[A-Za-z0-9_.-]{1,128}$/)
  .meta({
    id: "CallId",
    description:
      "Tool call id; may be provider-issued. The effect key is derived: <branch_id of the tool_call event>:<call_id>.",
  })
  .brand<"CallId">();
export type CallId = z.infer<typeof CallId>;

export const SandboxId: Brand<z.ZodString, "SandboxId"> =
  NonEmpty.brand<"SandboxId">();
export type SandboxId = z.infer<typeof SandboxId>;

export const SnapshotId: Brand<z.ZodString, "SnapshotId"> =
  NonEmpty.brand<"SnapshotId">();
export type SnapshotId = z.infer<typeof SnapshotId>;

export const OccurrenceId: Brand<z.ZodString, "OccurrenceId"> =
  NonEmpty.brand<"OccurrenceId">();
export type OccurrenceId = z.infer<typeof OccurrenceId>;
